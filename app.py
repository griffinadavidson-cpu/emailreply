import os
import json
import requests
import anthropic
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Persistent edit store — survives gunicorn worker cycling
PENDING_EDITS_FILE = "/tmp/pending_edits.json"

def load_pending_edits():
    try:
        with open(PENDING_EDITS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_pending_edits(data):
    try:
        with open(PENDING_EDITS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[pending_edits] Failed to save: {e}")

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


def fetch_instantly_email_uuid(lead_email: str, campaign_id: str) -> str:
    """Fetch the reply_to_uuid from Instantly API using lead email + campaign."""
    try:
        resp = requests.get(
            "https://api.instantly.ai/api/v2/emails",
            headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
            params={
                "campaign_id": campaign_id,
                "email": lead_email,
                "limit": 5,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        emails = data.get("items", data.get("data", []))
        if emails:
            for email in emails:
                from_addr = email.get("from_address") or email.get("from", {}).get("address", "")
                if from_addr == lead_email:
                    uuid = email.get("id") or email.get("uuid") or email.get("email_id")
                    if uuid:
                        print(f"[uuid] Found reply UUID: {uuid}")
                        return uuid
            # Fallback: return first email id
            first = emails[0]
            uuid = first.get("id") or first.get("uuid") or first.get("email_id", "")
            print(f"[uuid] Fallback UUID: {uuid}")
            return uuid
    except Exception as e:
        print(f"[uuid] Failed to fetch UUID: {e}")
    return ""


def send_instantly_reply(reply_to_uuid: str, eaccount: str, subject: str, body: str) -> dict:
    print(f"[instantly] Sending reply — uuid={reply_to_uuid} eaccount={eaccount} subject={subject}")
    resp = requests.post(
        "https://api.instantly.ai/api/v2/emails/reply",
        headers={
            "Authorization": f"Bearer {INSTANTLY_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "reply_to_uuid": reply_to_uuid,
            "eaccount": eaccount,
            "subject": subject,
            "body": {"html": body, "text": body},
        },
    )
    resp.raise_for_status()
    return resp.json()


# ============================================================
# ROUTE 1: Incoming email reply webhook (from Instantly)
# ============================================================

@app.route("/webhook/incoming", methods=["POST"])
def incoming_reply():
    data = request.json
    print(f"[RAW PAYLOAD] {json.dumps(data)}")
    body = data.get("body", data)

    lead_email = str(body.get("lead_email", ""))
    reply_snippet = body.get("reply_text_snippet", "")
    reply_text = body.get("reply_text", "")
    campaign_name = body.get("campaign_name", "")
    campaign_id = body.get("campaign_id", "")
    email_account = body.get("email_account", body.get("sending_account", ""))
    subject = body.get("reply_subject", body.get("subject", "Re:"))
    domain = lead_email.split("@")[1] if "@" in lead_email else ""

    # Extra lead info
    first_name = body.get("firstName", "")
    last_name = body.get("lastName", "")
    company_name = body.get("companyName", "")
    job_title = body.get("jobTitle", "")
    location = body.get("location", "")
    linkedin = body.get("linkedIn", "")

    # Step 1: Classify
    classification = classify_reply(reply_snippet)
    print(f"[classify] {lead_email} → {classification}")

    if classification != "INTERESTED":
        return jsonify({"status": "classified", "classification": classification}), 200

    # Step 2: Fetch UUID from Instantly API
    reply_to_uuid = fetch_instantly_email_uuid(lead_email, campaign_id)

    # Step 3: Sender name
    sender_name = extract_sender_name(email_account)

    # Step 4: Upsert Attio
    upsert_attio_company(domain)
    upsert_attio_person(lead_email)
    deal = create_attio_deal(lead_email)
    deal_id = deal["data"]["id"]["record_id"]

    # Step 5: Draft reply
    draft = draft_reply(sender_name, email_account, lead_email, campaign_name, reply_text)
    print(f"[draft] Generated {len(draft)} chars for {lead_email}")

    # Step 6: Build meta
    meta_send = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": email_account,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
    })
    meta_edit = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": email_account,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
    })
    meta_dismiss = json.dumps({
        "deal_id": deal_id,
        "lead_email": lead_email,
    })

    # Step 7: Slack blocks with full lead info
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"\U0001f514 *New Interested Reply*\n"
            f"*Campaign:* {campaign_name}\n"
            f"*Sender Account:* {email_account}\n"
            f"*Reply UUID:* `{reply_to_uuid or 'NOT FOUND'}`"}},
        {"type": "divider"},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Lead Email:*\n{lead_email}"},
            {"type": "mrkdwn", "text": f"*Name:*\n{first_name} {last_name}"},
            {"type": "mrkdwn", "text": f"*Company:*\n{company_name}"},
            {"type": "mrkdwn", "text": f"*Title:*\n{job_title}"},
            {"type": "mrkdwn", "text": f"*Location:*\n{location}"},
            {"type": "mrkdwn", "text": f"*LinkedIn:*\n{linkedin if linkedin else 'N/A'}"},
        ]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Subject:* {subject}\n\n*Their Reply:*\n{reply_snippet}"}},
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

    channel_id = payload.get("container", {}).get("channel_id", "")
    message_ts = payload.get("container", {}).get("message_ts", "")

    if action_id == "send_reply":
        draft = meta.get("draft", "")
        if meta.get("reply_to_uuid") and meta.get("eaccount"):
            send_instantly_reply(
                reply_to_uuid=meta["reply_to_uuid"],
                eaccount=meta["eaccount"],
                subject=meta.get("subject", "Re:"),
                body=draft,
            )
            print(f"[send_reply] Sent draft to {meta.get('lead_email')}")

        response_url = payload.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"\u2705 Reply sent to {meta.get('lead_email', 'lead')}",
            })
        return "", 200

    elif action_id == "edit_reply":
        post_slack_chat(
            channel_id,
            message_ts,
            f"\u270f\ufe0f *Edit the draft below and reply to this thread to send it.*\n\n{meta.get('draft', '')}"
        )

        # Save to file so it survives worker restarts
        pending = load_pending_edits()
        pending[message_ts] = {
            "reply_to_uuid": meta.get("reply_to_uuid"),
            "eaccount": meta.get("eaccount"),
            "subject": meta.get("subject", "Re:"),
            "lead_email": meta.get("lead_email"),
            "deal_id": meta.get("deal_id"),
        }
        save_pending_edits(pending)
        print(f"[edit_reply] Stored pending edit for thread {message_ts}")
        return "", 200

    elif action_id == "dismiss":
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

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]}), 200

    event = data.get("event", {})

    if event.get("bot_id") or not event.get("thread_ts"):
        return "", 200

    thread_ts = event["thread_ts"]
    channel = event["channel"]

    pending = load_pending_edits()
    meta = pending.get(thread_ts)
    if not meta:
        print(f"[slack_events] No pending edit for thread {thread_ts}")
        return "", 200

    thread = fetch_slack_thread(channel, thread_ts)
    messages = thread.get("messages", [])
    human_messages = [
        m for m in messages
        if not m.get("bot_id") and m.get("subtype") != "bot_message"
    ]
    if not human_messages:
        return "", 200

    reply_text = human_messages[-1].get("text", "")
    print(f"[slack_events] Sending edited reply for {meta.get('lead_email')} — uuid={meta.get('reply_to_uuid')}")

    send_instantly_reply(
        reply_to_uuid=meta["reply_to_uuid"],
        eaccount=meta["eaccount"],
        subject=meta["subject"],
        body=reply_text,
    )
    print(f"[edit_send] Sent edited reply to {meta['lead_email']}")

    del pending[thread_ts]
    save_pending_edits(pending)

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
