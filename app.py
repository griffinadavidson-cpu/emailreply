import os
import json
import re
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

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ============================================================
# HELPERS
# ============================================================

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
    Hit GET /v2/emails with campaign_id and lead.
    Returns (reply_to_uuid, eaccount):
      - reply_to_uuid: id from index 0 (most recent email)
      - eaccount:      eaccount from last item (original outbound send)
    Raises if nothing comes back.
    """
    resp = requests.get(
        "https://api.instantly.ai/api/v2/emails",
        headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
        params={
            "campaign_id": campaign_id,
            "lead": lead_email,
            "limit": 10,
        },
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
    print(f"[fetch_uuid] reply_to_uuid={uuid} eaccount={eaccount} for {lead_email}")
    return uuid, eaccount


def send_instantly_reply(reply_to_uuid: str, eaccount: str, subject: str, body: str) -> dict:
    payload = {
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "body": {"html": body, "text": body},
    }
    print(f"[send_reply] Payload: {json.dumps(payload)}")
    resp = requests.post(
        "https://api.instantly.ai/api/v2/emails/reply",
        headers={
            "Authorization": f"Bearer {INSTANTLY_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    print(f"[send_reply] Instantly status={resp.status_code} body={resp.text}")
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

    # Step 1: Classify the reply
    classification = classify_reply(reply_snippet)
    print(f"[classify] {lead_email} → {classification}")

    if classification != "INTERESTED":
        return jsonify({"status": "classified", "classification": classification}), 200

    # Step 2: Resolve the reply_to_uuid from Instantly (webhook doesn't include it)
    reply_to_uuid, eaccount = fetch_instantly_reply_uuid(campaign_id, lead_email)

    # Step 3: Extract sender name
    sender_name = extract_sender_name(eaccount)

    # Step 4: Upsert Attio company + person + deal
    upsert_attio_company(domain)
    upsert_attio_person(lead_email)
    deal = create_attio_deal(lead_email)
    deal_id = deal["data"]["id"]["record_id"]

    # Step 5: Draft reply with Claude
    draft = draft_reply(sender_name, eaccount, lead_email, campaign_name, reply_text)
    print(f"[draft] Generated {len(draft)} chars for {lead_email}")

    # Step 6: Build Slack message with action buttons
    meta_send = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
    })
    meta_edit = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
    })
    meta_dismiss = json.dumps({
        "deal_id": deal_id,
        "lead_email": lead_email,
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
        # Send the AI draft directly via Instantly
        draft = meta.get("draft", "")
        print(f"[slack_action] send_reply triggered. reply_to_uuid={meta.get('reply_to_uuid')} eaccount={meta.get('eaccount')} lead={meta.get('lead_email')}")
        if meta.get("reply_to_uuid") and meta.get("eaccount"):
            result = send_instantly_reply(
                reply_to_uuid=meta["reply_to_uuid"],
                eaccount=meta["eaccount"],
                subject=meta.get("subject", "Re:"),
                body=draft,
            )
            print(f"[send_reply] Instantly response: {result}")
        else:
            print(f"[send_reply] SKIPPED — missing reply_to_uuid or eaccount. meta keys: {list(meta.keys())}")

        # Acknowledge in Slack via response_url
        response_url = payload.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"\u2705 Reply sent to {meta.get('lead_email', 'lead')}",
            })

        return "", 200

    elif action_id == "edit_reply":
        # Post draft in a thread for editing
        eaccount_safe = meta.get("eaccount", "").replace("@", "[at]")
        lead_email_safe = meta.get("lead_email", "").replace("@", "[at]")
        thread_meta = json.dumps({
            "reply_to_uuid": meta.get("reply_to_uuid"),
            "eaccount": eaccount_safe,
            "subject": meta.get("subject", "Re:"),
            "lead_email": lead_email_safe,
            "deal_id": meta.get("deal_id"),
        })

        text = (
            f"\u270f\ufe0f *Edit the draft below and reply to this thread to send it.*\n\n"
            f"{meta.get('draft', '')}\n\n"
            f"META: {thread_meta}"
        )

        post_slack_chat(channel_id, message_ts, text)

        # Forward to n8n for edit & send flow
        requests.post(
            "https://n8n-xmux.onrender.com/webhook/366f0eeb-ec30-48e4-bfa7-5bead1c669a3",
            json={
                "reply_to_uuid": meta.get("reply_to_uuid"),
                "eaccount": meta.get("eaccount"),
                "subject": meta.get("subject", "Re:"),
                "lead_email": meta.get("lead_email"),
                "deal_id": meta.get("deal_id"),
                "draft": meta.get("draft", ""),
                "channel_id": channel_id,
                "message_ts": message_ts,
            },
        )
        print(f"[edit_reply] Forwarded to n8n for lead={meta.get('lead_email')}")
        return "", 200

    elif action_id == "dismiss":
        # Acknowledge dismissal
        response_url = payload.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"\u274c Dismissed reply from {meta.get('lead_email', 'lead')}",
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

    # Ignore bot messages, non-thread messages, and META system messages
    if event.get("bot_id") or not event.get("thread_ts") or "META:" in event.get("text", ""):
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

    def clean_email(raw):
        # Handle Slack mailto: auto-link: <mailto:x@y.com|x@y.com>
        if "mailto:" in raw:
            raw = raw.split("mailto:")[1].split("|")[0].strip()
        # Handle Slack URL auto-link on domain: x@<http://domain.com|domain.com>
        if "<http" in raw:
            local = raw.split("@")[0]
            domain_part = raw.split("@")[1]
            # extract domain from <http://domain.com|domain.com> → domain.com
            domain = domain_part.split("|")[-1].rstrip(">")
            raw = f"{local}@{domain}"
        raw = raw.replace("[at]", "@").strip()
        return raw

    eaccount = clean_email(meta.get("eaccount", ""))
    lead_email = clean_email(meta.get("lead_email", ""))

    # Send edited reply directly to Instantly
    print(f"[slack_events] sending to Instantly. reply_to_uuid={meta.get('reply_to_uuid')} eaccount={eaccount}")
    send_instantly_reply(
        reply_to_uuid=meta.get("reply_to_uuid", ""),
        eaccount=eaccount,
        subject=meta.get("subject", "Re:"),
        body=reply_text,
    )
    print(f"[edit_send] Sent edited reply to {lead_email}")

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
