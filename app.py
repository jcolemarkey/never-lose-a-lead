"""
Never Lose A Lead — Groundwork AI
Lead Response Agent Backend

Flow:
  Zapier POST → pull Airtable ontology → Claude SMS → Twilio send → log to Lead_Log
"""

import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from pyairtable import Api
from anthropic import Anthropic
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# App + logging
# ---------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
airtable       = Api(os.environ["AIRTABLE_API_KEY"])
anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client  = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)

BASE_ID          = os.environ["AIRTABLE_BASE_ID"]
TWILIO_FROM      = os.environ["TWILIO_FROM_NUMBER"]   # E.164 e.g. +18135550123
LOCAL_TZ         = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/New_York"))

ontology_table   = airtable.table(BASE_ID, "Groundwork_Ontology")
lead_log_table   = airtable.table(BASE_ID, "Lead_Log")


# ---------------------------------------------------------------------------
# 1. Pull business context from Airtable Groundwork_Ontology
# ---------------------------------------------------------------------------
def pull_ontology() -> str:
    """
    Fetch all active rows from Groundwork_Ontology and return them
    as a structured context string for the system prompt.
    """
    try:
        rows = ontology_table.all(formula="{Active}=1")
    except Exception as e:
        logger.error(f"Airtable ontology fetch failed: {e}")
        raise

    if not rows:
        logger.warning("Ontology table returned 0 active rows — check Active checkboxes.")

    sections = []
    for row in rows:
        f = row.get("fields", {})
        category   = f.get("Category", "General")
        key        = f.get("Context_Key", "")
        value      = f.get("Value", "").strip()
        examples   = f.get("Example_Phrases", "").strip()

        block = f"[{category}] {key}:\n{value}"
        if examples:
            block += f"\nExamples: {examples}"
        sections.append(block)

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 2. Build Claude system prompt
# ---------------------------------------------------------------------------
def build_system_prompt(ontology_context: str) -> str:
    """
    Compose the system prompt that tells Claude who it is, what to say,
    and exactly how to format the SMS.
    """
    # Callback times are always tomorrow in local timezone
    # Note: %-d (Linux) and %#d (Windows) are both non-portable — use .day instead
    _tomorrow = datetime.now(tz=LOCAL_TZ) + timedelta(days=1)
    tomorrow  = f"{_tomorrow.strftime('%A, %B')} {_tomorrow.day}"

    return f"""You are a fast-response agent for Groundwork AI. Someone just filled out \
our contact form. Your only job is to write a single SMS that makes them feel heard \
and locks in a callback.

## About Groundwork AI
{ontology_context}

## What the SMS must do — in this order
1. Use their first name
2. Acknowledge what they specifically asked about in 1 short phrase
3. Say we can help and that someone will call them
4. Offer two callback windows: 9am or 11am tomorrow ({tomorrow})
5. Tell them to reply 9 or 11 to confirm

## Hard rules
- 160 characters MAX — count carefully
- No emojis
- No "AI", "bot", "automated", or "system"
- No unsubscribe footers
- Write exactly like a business owner texting back, not a marketing tool
- If you cannot fit it in 160 characters, cut the middle — keep the name, topic, and callback ask

Return ONLY the SMS text. No quotes, no labels, no explanation."""


# ---------------------------------------------------------------------------
# 3. Generate SMS via Claude
# ---------------------------------------------------------------------------
def generate_sms(first_name: str, inquiry: str, ontology_context: str) -> str:
    """Call Claude and return the raw SMS string."""
    system_prompt = build_system_prompt(ontology_context)

    user_message = (
        f"Lead name: {first_name}\n"
        f"Their inquiry: {inquiry}\n\n"
        "Write the SMS now."
    )

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",   # Haiku: fastest, well under 60s
        max_tokens=100,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    sms_text = response.content[0].text.strip().strip('"')

    # Hard truncate to 160 as a safety net
    if len(sms_text) > 160:
        logger.warning(f"SMS exceeded 160 chars ({len(sms_text)}), truncating.")
        sms_text = sms_text[:157] + "..."

    return sms_text


# ---------------------------------------------------------------------------
# 4. Send SMS via Twilio
# ---------------------------------------------------------------------------
def normalize_phone(raw: str) -> str:
    """
    Accept any common US format and return E.164 (+1XXXXXXXXXX).
    Raises ValueError if the result is not 11 digits (1 + 10-digit number).
    """
    digits = "".join(filter(str.isdigit, raw))

    if len(digits) == 10:
        digits = "1" + digits

    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError(f"Cannot normalize phone number: '{raw}'")

    return f"+{digits}"


def send_sms(to_raw: str, body: str) -> str:
    """Send SMS and return the Twilio message SID."""
    to_e164 = normalize_phone(to_raw)

    message = twilio_client.messages.create(
        body=body,
        from_=TWILIO_FROM,
        to=to_e164,
    )
    return message.sid


# ---------------------------------------------------------------------------
# 5. Log lead to Airtable Lead_Log
# ---------------------------------------------------------------------------
def utcnow_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_submit_time(raw: str) -> datetime:
    """
    Try multiple formats Zapier/Typeform might send.
    Falls back to now() if unparseable.
    """
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    logger.warning(f"Could not parse submit_time '{raw}', using now.")
    return datetime.utcnow()


def log_lead(payload: dict, sms_text: str, sms_sent_iso: str) -> str:
    """Create a Lead_Log record and return the new Airtable record ID."""

    submit_raw  = payload.get("submit_time", utcnow_iso())
    submit_dt   = parse_submit_time(submit_raw)
    sent_dt     = parse_submit_time(sms_sent_iso)
    response_seconds = max(0, int((sent_dt - submit_dt).total_seconds()))

    # Airtable expects ISO 8601 for date/time fields
    submit_iso = submit_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    fields = {
        "First_Name":            payload.get("first_name", ""),
        "Last_Name":             payload.get("last_name", ""),
        "Phone":                 payload.get("phone", ""),
        "Email":                 payload.get("email", ""),
        "Source":                "Website Form",
        "Submit_Time":           submit_iso,
        "SMS_Sent_Time":         sms_sent_iso,
        "Response_Time_Seconds": response_seconds,
        "Raw_Inquiry_Text":      payload.get("inquiry", ""),
        "Inquiry_type":          "General Inquiry",   # Claude will refine this later
        "New_vs_Existing":       "Unknown",
        "Claude_Response":       sms_text,
        "Engagement":            "No Reply",
    }

    result = lead_log_table.create(fields)
    return result["id"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/trigger-sms", methods=["POST"])
def trigger_sms():
    """
    Main pipeline endpoint.
    Zapier POSTs lead data here → Claude → Twilio → Airtable.
    """
    try:
        payload = request.get_json(force=True, silent=True)
        if not payload:
            return jsonify({"error": "Empty or non-JSON request body"}), 400

        # --- Validate ---
        first_name = (payload.get("first_name") or "").strip()
        phone      = (payload.get("phone") or "").strip()
        inquiry    = (payload.get("inquiry") or "").strip()

        if not first_name or not phone:
            return jsonify({"error": "first_name and phone are required"}), 400

        if not inquiry:
            inquiry = "your inquiry"  # graceful fallback

        logger.info(f"Lead received → {first_name} | {phone}")

        # --- Step 1: Ontology ---
        logger.info("Fetching ontology context...")
        ontology_context = pull_ontology()

        # --- Step 2: Claude ---
        logger.info("Generating SMS via Claude...")
        sms_text = generate_sms(first_name, inquiry, ontology_context)
        logger.info(f"SMS ({len(sms_text)} chars): {sms_text}")

        # --- Step 3: Twilio ---
        sms_sent_iso = utcnow_iso()
        logger.info(f"Sending SMS to {phone}...")
        twilio_sid = send_sms(phone, sms_text)
        logger.info(f"SMS sent | SID: {twilio_sid}")

        # --- Step 4: Airtable ---
        logger.info("Logging to Airtable Lead_Log...")
        record_id = log_lead(payload, sms_text, sms_sent_iso)
        logger.info(f"Airtable record created | ID: {record_id}")

        return jsonify({
            "status":             "success",
            "lead":               first_name,
            "sms_sent_to":        phone,
            "twilio_sid":         twilio_sid,
            "airtable_record_id": record_id,
            "sms_preview":        sms_text,
            "sms_char_count":     len(sms_text),
        }), 200

    except ValueError as e:
        # Phone normalization or other validation errors
        logger.error(f"Validation error: {e}")
        return jsonify({"error": str(e)}), 422

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error — check logs"}), 500


@app.route("/health", methods=["GET"])
def health():
    """Quick liveness check. Hit this first after starting the server."""
    return jsonify({
        "status":    "ok",
        "timestamp": utcnow_iso(),
        "service":   "Groundwork AI — Lead Response Agent",
    }), 200


@app.route("/test-ontology", methods=["GET"])
def test_ontology():
    """
    Dev helper — returns raw ontology pull so you can verify
    Airtable rows are being read correctly before testing the full pipeline.
    """
    try:
        context = pull_ontology()
        return jsonify({"ontology": context}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, port=port)
