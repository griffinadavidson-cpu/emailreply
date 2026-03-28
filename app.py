import os
import json
import re
import time
import requests
import anthropic
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- Config ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")
ATTIO_OWNER_ID = os.getenv("ATTIO_OWNER_ID")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ============================================================
# HELPERS
# ============================================================

def clean_slack_email(email: str) -> str:
    """Strip Slack's auto-linking from email addresses.

    Slack auto-formats domains and emails into markup like:
      <http://claygeni.us|claygeni.us>
      <mailto:john@claygeni.us|john@claygeni.us>

    This function reverses all known Slack mangling patterns.
    """
    if not email:
        return ""
    # Reverse the [at] workaround first
    email = email.replace("[at]", "@")
    # Handle <http://domain.com|domain.com> and <https://...> format
    email = re.sub(r'<https?://([^|>]+)\|[^>]+>', r'\1', email)
    email = re.sub(r'<https?://([^>]+)>', r'\1', email)
    # Handle <mailto:email|email> format
    email = re.sub(r'<mailto:([^|>]+)\|[^>]+>', r'\1', email)
    email = re.sub(r'<mailto:([^>]+)>', r'\1', email)
    return email.strip()


def classify_reply(reply_snippet: str) -> str:
    """Use Claude Haiku to classify an email reply."""
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": (
                "Classify this email reply. Reply with ONLY one word: "
                "INTERESTED, NOT_INTERESTED, WRONG_PERSON, AUTO_REPLY, or UNSUBSCRIBE.\n\n"
                f"Reply: {reply_snippet}"
            ),
        }],
    )
    return msg.content[0].text.strip()


def draft_reply(sender_name: str, email_account: str, lead_email: str,
                campaign_name: str, reply_text: str) -> str:
    """Use Claude Haiku to draft an email reply for INTERESTED leads."""
    prompt = f"""You are an AI assistant helping draft email replies for an M&A outreach campaign.

ROLE: You are writing on behalf of the SENDER (the rep). The SENDER is reaching out to the PROSPECT to discuss a potential business acquisition or advisory relationship.

SENDER (the rep writing this email): {sender_name} — sending from {email_account}
PROSPECT (the person who replied): {lead_email}

COMPANY IDENTIFICATION - read the sending email domain:
- If the sending email contains "state17", "findstate17" or similar → you represent STATE17
- If the sending email contains "options2exit", "o2e" or similar → you represent OPTIONS2EXIT
- If neither matches → use the campaign name to determine context

STATE17 CONTEXT:
State17 is a family office based in Florida with offices in New York City. They are a direct buyer actively acquiring home service businesses including roofing, windows, doors, siding, garage doors, and fencing companies. They are not a broker — they buy and operate businesses themselves. The conversation should be buyer-to-seller.

OPTIONS2EXIT CONTEXT:
Options2Exit is a sell-side advisory firm. They help business owners who are considering exiting or transitioning out of a business they built. They are not the buyer — they represent the seller and help them get the best outcome. The conversation should be advisor-to-owner, focused on understanding the owner's goals and timeline.

REPLY INSTRUCTIONS:
Write a short, professional reply under 120 words that directly addresses what the prospect said. Be conversational, no corporate fluff. Write FROM the sender TO the prospect.

- If they want to schedule a call → confirm enthusiasm, suggest they pick a time
- If they expressed general interest → ask 3 qualifying questions: approximate annual revenue, years of ownership, and what's prompting their interest in potentially selling
- If they asked what you do → explain clearly based on which company you represent, then move toward a call
- If they asked a specific question → answer it accurately based on the correct company context, then advance the conversation

Sign off with this name exactly: {sender_name}
Do not include a subject line or any "Subject:" prefix. Output only the reply body.

Campaign: {campaign_name}
Full email thread: {reply_text}"""

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def extract_sender_name(email_account: str) -> str:
    """Extract first name from email, e.g. john.tanner@x.com → John"""
    local = email_account.split("@")[0]
    first = local.split(".")[0]
    return first.capitalize()


def upsert_attio_company(domain: str) -> dict:
    resp = requests.put(
        "https://api.attio.com/v2/objects/companies/records",
        params={"matching_attribute": "domains"},
        headers={"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"},
        json={"data": {"values": {"domains": [{"domain": domain}]}}},
    )
    resp.raise_for_status()
    return resp.json()


def upsert_attio_person(lead_email: str) -> dict:
    resp = requests.put(
        "https://api.attio.com/v2/objects/people/records",
        params={"matching_attribute": "email_addresses"},
        headers={"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"},
        json={"data": {"values": {"email_addresses": [{"email_address": lead_email}]}}},
    )
    resp.raise_for_status()
    return resp.json()


def create_attio_deal(lead_email: str) -> dict:
    resp = requests.post(
        "https://api.attio.com/v2/objects/deals/records",
        headers={"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"},
        json={
            "data": {
                "values": {
                    "name": [{"value": f"{lead_email} - Interested"}],
                    "stage": [{"status": "In Progress"}],
                    "owner": [{
                        "referenced_actor_type": "workspace-member",
                        "referenced_actor_id": ATTIO_OWNER_ID,
                    }],
                }
            }
        },
    )
    resp.raise_for_status()
    return resp.json()


def send_slack_message(blocks: list) -> dict:
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json={"blocks": blocks},
    )
    resp.raise_for_status()
    return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"ok": True}


def post_slack_chat(channel: str, thread_ts: str, text: str) -> dict:
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"channel": channel, "thread_ts": thread_ts, "text": text},
    )
    resp.raise_for_status()
    return resp.json()


def fetch_slack_thread(channel: str, ts: str) -> dict:
    resp = requests.get(
        "https://slack.com/api/conversations.replies",
        params={"channel": channel, "ts": ts, "limit": 20},
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    resp.raise_for_status()
    return resp.json()


def fetch_instantly_reply_uuid(campaign_id: str, lead_email: str) -> tuple:
    """
    Hit GET /v2/emails with campaign_id, lead, and optionally eaccount.
    Returns (reply_to_uuid, eaccount, thread_html, timestamp_email, from_address):
      - reply_to_uuid: id from index 0 (most recent email)
      - eaccount:      eaccount from last item (original outbound send)
      - thread_html:   HTML body from index 0 (for threading)
      - timestamp_email: timestamp of most recent email
      - from_address:  sender of most recent email
    Raises if nothing comes back.
    """
    params = {
        "lead": lead_email,
        "limit": 10,
    }
    if campaign_id:
        params["campaign_id"] = campaign_id

    resp = requests.get(
        "https://api.instantly.ai/api/v2/emails",
        headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()

    emails = data.get("items", data) if isinstance(data, dict) else data
    if not emails:
        raise ValueError(
            f"[fetch_uuid] No emails found for lead={lead_email} campaign={campaign_id}"
        )

    uuid = emails[0].get("id")
    eaccount = emails[-1].get("eaccount")
    timestamp_email = emails[0].get("timestamp_email", "")
    # Who sent the most recent email (the one we're quoting)
    from_address = emails[0].get("from_address_email", emails[0].get("eaccount", ""))

    # Grab thread HTML for proper email threading
    body_obj = emails[0].get("body", {})
    if isinstance(body_obj, dict):
        thread_html = body_obj.get("html", "")
    else:
        thread_html = str(body_obj) if body_obj else ""

    print(f"[fetch_uuid] reply_to_uuid={uuid} eaccount={eaccount} thread_html_len={len(thread_html)} for {lead_email}")
    return uuid, eaccount, thread_html, timestamp_email, from_address


def send_instantly_reply(reply_to_uuid: str, eaccount: str, subject: str,
                         body: str, thread_html: str = "",
                         timestamp_email: str = "", from_address: str = "") -> dict:
    """Send a reply via Instantly with proper HTML threading."""
    from datetime import datetime

    # Convert plain text newlines to HTML breaks
    html_body = body.replace("\n", "<br>")

    full_html = f"<div>{html_body}</div>"
    if thread_html:
        # Format the timestamp from Instantly's timestamp_email field
        wrote_line = ""
        if timestamp_email:
            try:
                dt = datetime.fromisoformat(timestamp_email.replace("Z", "+00:00"))
                formatted_ts = dt.strftime("%a, %b %d, %Y at %I:%M %p")
                sender = from_address or eaccount
                wrote_line = f'On {formatted_ts} {sender} wrote:'
            except Exception:
                wrote_line = ""

        if wrote_line:
            full_html += (
                f'<br><div class="gmail_quote">'
                f'<div dir="ltr" class="gmail_attr">{wrote_line}<br></div>'
                f'<blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex;border-left:1px solid rgb(204,204,204);padding-left:1ex">'
                f'{thread_html}'
                f'</blockquote></div>'
            )
        else:
            full_html += f"<br>{thread_html}"

    payload = {
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "body": {"html": full_html},
    }
    print(f"[send_reply] Payload: {json.dumps(payload)[:500]}")
    resp = requests.post(
        "https://api.instantly.ai/api/v2/emails/reply",
        headers={
            "Authorization": f"Bearer {INSTANTLY_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    print(f"[send_reply] Instantly status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()


# ============================================================
# ROUTE 1: Incoming email reply webhook (from Instantly)
# ============================================================

@app.route("/webhook/incoming", methods=["POST"])
def incoming_reply():
    data = request.json
    body = data.get("body", data)  # support both nested and flat

    lead_email = str(body.get("lead_email", ""))

    reply_snippet = body.get("reply_text_snippet", "")
    reply_text = body.get("reply_text", "")
    campaign_name = body.get("campaign_name", "")
    campaign_id = body.get("campaign_id", "")

    subject = body.get("reply_subject") or body.get("subject") or "Re:"
    domain = lead_email.split("@")[1] if "@" in lead_email else ""

    # Step 1: Resolve the reply_to_uuid + thread HTML from Instantly
    # Retry once after 3 seconds if Instantly hasn't indexed the email yet
    try:
        reply_to_uuid, eaccount, thread_html, timestamp_email, from_address = fetch_instantly_reply_uuid(campaign_id, lead_email)
    except ValueError:
        print(f"[incoming] No emails found on first try for {lead_email}. Retrying in 3s...")
        time.sleep(3)
        try:
            reply_to_uuid, eaccount, thread_html, timestamp_email, from_address = fetch_instantly_reply_uuid(campaign_id, lead_email)
        except ValueError as e:
            print(f"[incoming] Still no emails after retry for {lead_email}. Skipping. Error: {e}")
            return jsonify({"status": "skipped", "reason": "no_emails_found"}), 200

    # Step 2: Extract sender name
    sender_name = extract_sender_name(eaccount)

    # Step 3: Upsert Attio company + person + deal
    upsert_attio_company(domain)
    upsert_attio_person(lead_email)
    deal = create_attio_deal(lead_email)
    deal_id = deal["data"]["id"]["record_id"]

    # Step 4: Draft reply with Claude
    draft = draft_reply(sender_name, eaccount, lead_email, campaign_name, reply_text)
    print(f"[draft] Generated {len(draft)} chars for {lead_email}")

    # Step 5: Build Slack message with action buttons
    meta_send = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
        "thread_html": thread_html,
        "timestamp_email": timestamp_email,
        "from_address": from_address,
    })
    meta_edit = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
        "campaign_id": campaign_id,
        "thread_html": thread_html,
    })
    meta_dismiss = json.dumps({
        "deal_id": deal_id,
        "lead_email": lead_email,
    })

    # Check if meta payloads exceed Slack's 2000 char limit for button values
    # If thread_html is too large, store it separately and skip in meta
    if len(meta_send) > 1900 or len(meta_edit) > 1900:
        print(f"[warn] Meta payload too large for Slack buttons (send={len(meta_send)}, edit={len(meta_edit)}). Dropping thread_html from button meta.")
        meta_send = json.dumps({
            "reply_to_uuid": reply_to_uuid,
            "eaccount": eaccount,
            "subject": subject,
            "lead_email": lead_email,
            "deal_id": deal_id,
            "draft": draft,
            "campaign_id": campaign_id,
            "refetch_thread": True,
        })
        meta_edit = json.dumps({
            "reply_to_uuid": reply_to_uuid,
            "eaccount": eaccount,
            "subject": subject,
            "lead_email": lead_email,
            "deal_id": deal_id,
            "draft": draft,
            "campaign_id": campaign_id,
            "refetch_thread": True,
        })

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"\U0001f514 *New Interested Reply*\n"
            f"*Campaign:* {campaign_name}\n"
            f"*Sender:* {eaccount}\n"
            f"*Lead:* {lead_email}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Lead's Reply:*\n{reply_snippet}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*AI Draft:*\n{draft}"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "\u2705 Send Reply"},
             "style": "primary", "action_id": "send_reply", "value": meta_send},
            {"type": "button", "text": {"type": "plain_text", "text": "\u270f\ufe0f Edit & Send"},
             "action_id": "edit_reply", "value": meta_edit},
            {"type": "button", "text": {"type": "plain_text", "text": "\u274c Dismiss"},
             "style": "danger", "action_id": "dismiss", "value": meta_dismiss},
        ]},
    ]

    send_slack_message(blocks)

    return jsonify({"status": "interested", "deal_id": deal_id}), 200


# ============================================================
# ROUTE 2: Slack interactive actions (button clicks)
# ============================================================

@app.route("/webhook/slack-actions", methods=["POST"])
def slack_actions():
    payload = json.loads(request.form.get("payload", "{}"))
    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    meta = json.loads(action.get("value", "{}"))
    print(f"[slack_action] action_id={action_id} reply_to_uuid={meta.get('reply_to_uuid')} eaccount={meta.get('eaccount')} lead={meta.get('lead_email')}")

    channel_id = payload.get("container", {}).get("channel_id", "")
    message_ts = payload.get("container", {}).get("message_ts", "")

    if action_id == "send_reply":
        draft = meta.get("draft", "")
        thread_html = meta.get("thread_html", "")
        timestamp_email = meta.get("timestamp_email", "")
        from_address = meta.get("from_address", "")

        # If thread_html was too large for Slack button, refetch it
        if meta.get("refetch_thread") and meta.get("campaign_id"):
            try:
                _, _, thread_html, timestamp_email, from_address = fetch_instantly_reply_uuid(
                    meta["campaign_id"], clean_slack_email(meta.get("lead_email", ""))
                )
            except Exception as e:
                print(f"[send_reply] Failed to refetch thread_html: {e}")

        eaccount = clean_slack_email(meta.get("eaccount", ""))
        lead_email = clean_slack_email(meta.get("lead_email", ""))

        print(f"[slack_action] send_reply triggered. reply_to_uuid={meta.get('reply_to_uuid')} eaccount={eaccount} lead={lead_email}")
        if meta.get("reply_to_uuid") and eaccount:
            result = send_instantly_reply(
                reply_to_uuid=meta["reply_to_uuid"],
                eaccount=eaccount,
                subject=meta.get("subject", "Re:"),
                body=draft,
                thread_html=thread_html,
                timestamp_email=timestamp_email,
                from_address=from_address,
            )
            print(f"[send_reply] Instantly response: {result}")
        else:
            print(f"[send_reply] SKIPPED — missing reply_to_uuid or eaccount. meta keys: {list(meta.keys())}")

        # Acknowledge in Slack via response_url
        response_url = payload.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"\u2705 Reply sent to {lead_email}",
            })

        return "", 200

    elif action_id == "edit_reply":
        eaccount = clean_slack_email(meta.get("eaccount", ""))
        lead_email = clean_slack_email(meta.get("lead_email", ""))

        thread_meta = json.dumps({
            "reply_to_uuid": meta.get("reply_to_uuid"),
            "eaccount": eaccount,
            "subject": meta.get("subject", "Re:"),
            "lead_email": lead_email,
            "deal_id": meta.get("deal_id"),
            "campaign_id": meta.get("campaign_id", ""),
        })

        text = (
            f"\u270f\ufe0f *Edit the draft below and reply to this thread to send it.*\n\n"
            f"{meta.get('draft', '')}\n\n"
            f"META: {thread_meta}"
        )

        post_slack_chat(channel_id, message_ts, text)

        # Don't forward to n8n here — wait for user to reply in thread
        # n8n webhook fires from /webhook/slack-events when user actually sends
        print(f"[edit_reply] Posted draft to Slack thread for lead={lead_email}. Waiting for user reply.")
        return "", 200

    elif action_id == "dismiss":
        lead_email = clean_slack_email(meta.get("lead_email", ""))
        response_url = payload.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"\u274c Dismissed reply from {lead_email}",
            })
        return "", 200

    return "", 200


# ============================================================
# ROUTE 3: Slack events (thread replies for edited drafts)
# ============================================================

@app.route("/webhook/slack-events", methods=["POST"])
def slack_events():
    data = request.json

    # Handle Slack URL verification challenge
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]}), 200

    event = data.get("event", {})

    # Ignore bot messages and non-thread messages
    if event.get("bot_id") or not event.get("thread_ts"):
        return "", 200

    thread_ts = event["thread_ts"]
    channel = event["channel"]

    # Fetch the thread to find the META data and the user's edited reply
    thread = fetch_slack_thread(channel, thread_ts)
    messages = thread.get("messages", [])

    # Get the last human (non-bot) message as the edited reply
    human_messages = [m for m in messages if not m.get("bot_id") and m.get("subtype") != "bot_message" and "META:" not in m.get("text", "")]
    if not human_messages:
        return "", 200
    reply_text = human_messages[-1].get("text", "")

    # Find the META message
    meta_message = None
    for m in reversed(messages):
        if m.get("text") and "META:" in m["text"]:
            meta_message = m
            break

    if not meta_message:
        return "", 200

    meta_match = re.search(r"META: ({.+})", meta_message["text"])
    if not meta_match:
        return "", 200

    meta = json.loads(meta_match.group(1))

    # Clean up all email fields using the universal cleaner
    eaccount = clean_slack_email(meta.get("eaccount", ""))
    lead_email = clean_slack_email(meta.get("lead_email", ""))

    # Send directly via Instantly (no n8n middleman)
    print(f"[slack_events] Sending edited reply directly. reply_to_uuid={meta.get('reply_to_uuid')} eaccount={eaccount} lead={lead_email} body_preview={reply_text[:80]}")

    try:
        # Fetch thread HTML for proper threading
        thread_html = ""
        timestamp_email = ""
        from_address = ""
        campaign_id = meta.get("campaign_id", "")
        if campaign_id and lead_email:
            try:
                _, _, thread_html, timestamp_email, from_address = fetch_instantly_reply_uuid(campaign_id, lead_email)
            except Exception as e:
                print(f"[slack_events] Could not fetch thread_html: {e}")

        result = send_instantly_reply(
            reply_to_uuid=meta.get("reply_to_uuid", ""),
            eaccount=eaccount,
            subject=meta.get("subject", "Re:"),
            body=reply_text,
            thread_html=thread_html,
            timestamp_email=timestamp_email,
            from_address=from_address,
        )
        print(f"[edit_send] Instantly response: {result}")

        # Confirm in Slack thread
        post_slack_chat(channel, thread_ts, f"\u2705 Reply sent to {lead_email}")

    except Exception as e:
        print(f"[edit_send] Failed to send reply: {e}")
        post_slack_chat(channel, thread_ts, f"\u274c Failed to send reply: {str(e)}")

    return "", 200


# ============================================================
# Health check
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "email-reply-bot"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
