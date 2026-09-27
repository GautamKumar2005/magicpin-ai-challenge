import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import composer, config
from app.models import ContextPush, ReplyRequest, TickRequest
from app.store import context_store, conversation_store
from app.utils import classify_incoming_message

app = FastAPI(title="magicpin AI Challenge — Vera+ bot")
START_TIME = time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# GET /v1/healthz
# --------------------------------------------------------------------------
@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": context_store.counts(),
    }


# --------------------------------------------------------------------------
# GET /v1/metadata
# --------------------------------------------------------------------------
@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": config.TEAM_NAME,
        "team_members": config.TEAM_MEMBERS,
        "model": config.MODEL if config.GEMINI_API_KEY else "template-fallback (no API key set)",
        "approach": config.APPROACH,
        "contact_email": config.CONTACT_EMAIL,
        "version": config.BOT_VERSION,
        "submitted_at": _now_iso(),
    }


# --------------------------------------------------------------------------
# POST /v1/context
# --------------------------------------------------------------------------
@app.post("/v1/context")
async def push_context(request: Request):
    raw = await request.body()
    if len(raw) > config.MAX_CONTEXT_PAYLOAD_BYTES:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "payload_too_large", "details": f"{len(raw)} bytes"},
        )
    try:
        body = ContextPush.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_body", "details": str(exc)},
        )

    accepted, reason, current_version = context_store.push(
        body.scope, body.context_id, body.version, body.payload
    )
    if not accepted:
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": reason, "current_version": current_version},
        )
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------
# POST /v1/tick
# --------------------------------------------------------------------------
@app.post("/v1/tick")
async def tick(body: TickRequest):
    actions: list[dict] = []

    for trigger_id in body.available_triggers:
        if len(actions) >= config.MAX_ACTIONS_PER_TICK:
            break

        trigger = context_store.get("trigger", trigger_id)
        if not trigger:
            continue

        suppression_key = trigger.get("suppression_key", "") or ""
        if suppression_key and conversation_store.suppression_key_used(suppression_key):
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = context_store.get("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue  # can't ground a message without the merchant context

        category_slug = merchant.get("category_slug")
        category = context_store.get("category", category_slug) if category_slug else None

        customer_id = trigger.get("customer_id")
        customer = context_store.get("customer", customer_id) if customer_id else None

        conversation_id = f"conv_{merchant_id}_{trigger_id}"
        if conversation_store.get(conversation_id) is not None:
            continue  # already started for this trigger

        composed = composer.compose_proactive(category, merchant, trigger, customer, already_sent=[])

        send_as = "merchant_on_behalf" if customer else "vera"
        conversation_store.create(conversation_id, merchant_id, customer_id, category_slug, trigger_id)
        conversation_store.record_bot_turn(conversation_id, composed["body"], composed["cta"])
        if suppression_key:
            conversation_store.mark_suppression_key_used(suppression_key)

        merchant_name = (merchant.get("identity", {}) or {}).get("name", merchant_id)
        actions.append(
            {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": send_as,
                "trigger_id": trigger_id,
                "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
                "template_params": [merchant_name, trigger.get("kind", "")],
                "body": composed["body"],
                "cta": composed["cta"],
                "suppression_key": suppression_key,
                "rationale": composed["rationale"],
            }
        )

    return {"actions": actions}


AUTO_REPLY_KEYWORDS = [
    "thank you for reaching out", "we have received your message",
    "will get back to you", "out of office", "auto-reply", "automated message",
    "this is an automated response", "thank you for contacting", "we will contact you shortly"
]

def contains_hindi(text: str) -> bool:
    hindi_keywords = {"theek", "sahi", "karo", "haan", "bhejo", "chalo", "samajh", "aage", "hoon"}
    words = set(text.lower().split())
    return bool(words.intersection(hindi_keywords))
# --------------------------------------------------------------------------
# POST /v1/reply
# --------------------------------------------------------------------------
@app.post("/v1/reply")
async def reply(body: ReplyRequest):
    conv = conversation_store.get(body.conversation_id)
    if conv is None:
        # Standalone replay scenarios may call /v1/reply without a prior
        # /v1/tick for this exact conversation_id. Recover gracefully.
        merchant = context_store.get("merchant", body.merchant_id) if body.merchant_id else None
        category_slug = merchant.get("category_slug") if merchant else None
        conv = conversation_store.create(
            body.conversation_id, body.merchant_id, body.customer_id, category_slug, None
        )

    if conv.get("ended"):
        return {"action": "end", "rationale": "This conversation was already ended."}

    merchant = context_store.get("merchant", conv["merchant_id"]) if conv["merchant_id"] else None
    category = context_store.get("category", conv.get("category_slug")) if conv.get("category_slug") else None
    customer = context_store.get("customer", conv.get("customer_id")) if conv.get("customer_id") else None

    repeat_count = conversation_store.record_incoming_turn(body.conversation_id, body.from_role, body.message)
    classification = classify_incoming_message(body.message, repeat_count)
    # 1. Immediate keyword check for canned OOO / automated replies
    lowered_msg = body.message.lower()
    if any(keyword in lowered_msg for keyword in AUTO_REPLY_KEYWORDS):
        if conv.get("autoreply_probed"):
            conv["ended"] = True
            return {
                "action": "end",
                "rationale": "Automated message keyword detected after probe; exiting gracefully."
            }
    if classification == "autoreply_confirmed":
        conv["ended"] = True
        return {
            "action": "end",
            "rationale": (
                "Same message seen 3+ times verbatim — this is the merchant's WhatsApp "
                "Business canned auto-reply, not a human. Exiting gracefully instead of "
                "burning further turns."
            ),
        }

    if classification == "autoreply_probe":
        if conv.get("autoreply_probed"):
            # Probed once already and it happened again — treat as confirmed.
            conv["ended"] = True
            return {
                "action": "end",
                "rationale": "Auto-reply pattern persisted after one probe; exiting gracefully.",
            }
        conv["autoreply_probed"] = True
        directive = (
            "This reply looks like a canned WhatsApp Business auto-reply (forwarding-to-team "
            "language), not a real human response. Send ONE short, low-friction message asking "
            "if the merchant/owner personally wants to quickly check this themselves (frame it "
            "as a 2-minute ask). Do not repeat your first message."
        )
        composed = composer.compose_reply(category, merchant, customer, conv, body.message, directive)
        conversation_store.record_bot_turn(body.conversation_id, composed["body"], composed["cta"])
        return {"action": "send", "body": composed["body"], "cta": composed["cta"], "rationale": composed["rationale"]}

    if classification == "decline":
        conv["ended"] = True
        return {
            "action": "end",
            "rationale": "Merchant explicitly declined / opted out. Exiting gracefully, no further nudges.",
        }

    if classification == "wait":
        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": "Merchant asked for time; backing off 30 minutes before re-engaging.",
        }

    if classification == "hostile":
        directive = (
            "The merchant's message is hostile/abusive, or raises something unrelated to "
            "magicpin/GBP/marketing (e.g. an unrelated request). Stay calm and polite, do not "
            "escalate or over-apologize. If it's an unrelated ask, briefly note you can't help "
            "with that specific thing here, then steer back to the original topic in the same "
            "message."
        )
        composed = composer.compose_reply(category, merchant, customer, conv, body.message, directive)
        conversation_store.record_bot_turn(body.conversation_id, composed["body"], composed["cta"])
        return {"action": "send", "body": composed["body"], "cta": composed["cta"], "rationale": composed["rationale"]}

    if classification == "intent_confirm":
        if contains_hindi(body.message):
            body_text = (
                "Great! Onboarding aage badhane ke liye, kripya apni "
                "GST details aur outlet address confirm karein."
            )
            cta_text = "Submit Business Details"
        else:
            body_text = (
                "Great! To move forward with onboarding, please share your "
                "GST registration details and primary outlet address."
            )
            cta_text = "Submit Business Details"

        conversation_store.record_bot_turn(
            body.conversation_id, body_text, cta_text
        )

        return {
            "action": "send",
            "body": body_text,
            "cta": cta_text,
            "rationale": "Merchant confirmed intent; providing concrete next step in matching language."
        }
        # normal
        directive = (
            "Continue the conversation naturally, advancing toward the original trigger's goal, "
            "and directly acknowledge what the merchant just said."
        )
        composed = composer.compose_reply(category, merchant, customer, conv, body.message, directive)
        conversation_store.record_bot_turn(body.conversation_id, composed["body"], composed["cta"])
        return {"action": "send", "body": composed["body"], "cta": composed["cta"], "rationale": composed["rationale"]}


# --------------------------------------------------------------------------
# POST /v1/teardown (optional, per testing brief §11)
# --------------------------------------------------------------------------
@app.post("/v1/teardown")
async def teardown():
    context_store.wipe()
    conversation_store.wipe()
    return {"status": "wiped"}


@app.get("/")
async def root():
    return {"service": "magicpin AI Challenge bot", "endpoints": [
        "GET /v1/healthz", "GET /v1/metadata", "POST /v1/context", "POST /v1/tick",
        "POST /v1/reply", "POST /v1/teardown",
    ]}
