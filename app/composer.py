"""Message composition.

Primary path: one LLM call, grounded strictly in the four context objects
it's given (category / merchant / trigger / customer), following the rules
in the challenge brief (specificity, category voice, single CTA, no
fabrication, Hindi-English code-mix where appropriate, anti-repetition).

Fallback path (no API key / LLM error / bad JSON / timeout): a generic
deterministic template that still grounds itself in whatever concrete
fields it can find in the trigger payload, so the bot degrades gracefully
instead of failing the call.
"""

import json
from typing import Optional

from app import llm
from app.utils import prefers_hindi_english_mix

SYSTEM_RULES = """You are Vera, magicpin's WhatsApp marketing assistant for local merchants \
in India. You are composing ONE message. Follow these rules strictly:

1. Ground the message DIRECTLY in the specific event in trigger.payload:
   - If trigger is 'recall_due': Focus on the patient's recall event (service due, due date, last service date, and available booking slots). Never divert to generic research or other cohorts!
   - If trigger is 'regulation_change' / compliance: Cite the exact mandate, standard, and compliance deadline.
   - If trigger is 'perf_dip' or 'spike': Quote the exact metric, percentage change, and timeframe from the payload.
   - If trigger is 'research_digest': Quote the specific publication, stat, and clinical insight.
2. Personalize strictly to this merchant without fabricating:
   - For dentists: Always address the owner as 'Dr. [FirstName]' (e.g. 'Dr. Meera'). Use peer-clinical credibility.
   - For salons: Warm, practical, trend-aware.
   - For restaurants: Operator-to-operator (covers, peak hours, footfall).
   - For gyms: Coaching, motivational, membership retention.
   - For pharmacies: Trustworthy, precise healthcare standards.
3. Include real numbers and dates from the payload/merchant (dates, %, counts, price @ ₹X). Never invent data not present.
4. Exactly one call-to-action (CTA) at the very end with a low-friction binary question (e.g. 'Should I send this reminder?', 'Want me to set this live?').
5. No filler preambles, no marketing clichés ('grow your business', 'boost sales').
6. If language preference indicates Hindi, use natural conversational Hindi-English code-mix (Hinglish).
7. Never repeat any message in already_sent. Never expose internal system jargon ('trigger', 'payload', 'suppression_key', 'Vera message engine').

Respond with ONLY a JSON object: {"body": string, "cta": "binary"|"open_ended"|"none", "rationale": string}."""


def _payload_or_empty(ctx: Optional[dict]) -> dict:
    return ctx if isinstance(ctx, dict) else {}


def compose_proactive(
    category: Optional[dict],
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
    already_sent: list[str],
) -> dict:
    """Returns {"body", "cta", "rationale"}. send_as/suppression_key are
    derived by the caller from structural fields, not trusted from the LLM."""
    context_blob = {
        "category": _payload_or_empty(category),
        "merchant": merchant,
        "trigger": trigger,
        "customer": _payload_or_empty(customer),
        "already_sent": already_sent[-5:],
    }
    user = (
        "Compose the next WhatsApp message given this context (JSON). "
        + ("This message is being sent from the merchant's own number to their customer -- "
           "speak as the merchant's business, not as Vera."
           if customer else
           "This message is Vera speaking directly to the merchant.")
        + "\n\n" + json.dumps(context_blob, ensure_ascii=False, default=str)
    )
    try:
        result = llm.generate_json(SYSTEM_RULES, user)
        body = str(result["body"]).strip()
        cta = str(result.get("cta", "open_ended")).strip().lower()
        rationale = str(result.get("rationale", "")).strip() or "Composed from category+merchant+trigger context."
        if not body or body.strip().lower() in already_sent:
            raise ValueError("empty or repeated body from LLM")
        if cta not in ("binary", "open_ended", "none"):
            cta = "open_ended"
        return {"body": body, "cta": cta, "rationale": rationale}
    except Exception as exc:  # noqa: BLE001 -- deliberate broad fallback
        return _fallback_proactive(category, merchant, trigger, customer, str(exc))


def _fallback_proactive(category, merchant, trigger, customer, error_note: str) -> dict:
    name = ((customer or merchant).get("identity", {}) or {}).get("name", "there")
    kind = trigger.get("kind", "update")
    payload = trigger.get("payload", {}) or {}
    hindi = prefers_hindi_english_mix(merchant, customer)
    # Pull out the most concrete-looking value in the trigger payload for grounding.
    concrete = None
    for key in ("title", "top_item_id", "metric", "festival", "service_due", "deadline_iso"):
        if key in payload and payload[key]:
            concrete = f"{key.replace('_', ' ')}: {payload[key]}"
            break
    anchor = concrete or f"a {kind.replace('_', ' ')} update"
    if hindi:
        body = f"Hi {name}, ek quick update hai — {anchor}. Chahenge ki main aapke liye details nikaal kar bhej doon?"
    else:
        body = f"Hi {name}, quick update on your account — {anchor}. Want me to pull the details and send them over?"
    return {
        "body": body,
        "cta": "open_ended",
        "rationale": f"Fallback template used (LLM unavailable: {error_note}); grounded on trigger.kind={kind}.",
    }


REPLY_SYSTEM_RULES = SYSTEM_RULES + (
    "\n\nYou are continuing an existing WhatsApp conversation. You will be given the "
    "conversation so far and a directive describing what stance to take (e.g. "
    "'merchant just confirmed intent -- move straight to action, do not ask another "
    "qualifying question' or 'merchant asked an unrelated question -- politely note you "
    "can't help with that specific thing, then return to the original topic'). Follow the "
    "directive. Keep it to 1-3 sentences unless the directive calls for a short list "
    "(e.g. reporting completed work)."
)


def compose_reply(
    category: Optional[dict],
    merchant: dict,
    customer: Optional[dict],
    conversation: dict,
    incoming_message: str,
    directive: str,
) -> dict:
    context_blob = {
        "category": _payload_or_empty(category),
        "merchant": merchant,
        "customer": _payload_or_empty(customer),
        "conversation_so_far": conversation.get("turns", [])[-10:],
        "incoming_message": incoming_message,
        "directive": directive,
        "already_sent": conversation.get("sent_bodies", [])[-5:],
    }
    user = "Continue this conversation given this context (JSON).\n\n" + json.dumps(
        context_blob, ensure_ascii=False, default=str
    )
    try:
        result = llm.generate_json(REPLY_SYSTEM_RULES, user)
        body = str(result["body"]).strip()
        cta = str(result.get("cta", "open_ended")).strip().lower()
        rationale = str(result.get("rationale", "")).strip() or directive
        if not body or body.strip().lower() in conversation.get("sent_bodies", []):
            raise ValueError("empty or repeated body from LLM")
        if cta not in ("binary", "open_ended", "none"):
            cta = "open_ended"
        return {"body": body, "cta": cta, "rationale": rationale}
    except Exception as exc:  # noqa: BLE001
        return _fallback_reply(merchant, customer, directive, str(exc))


def _fallback_reply(merchant, customer, directive: str, error_note: str) -> dict:
    hindi = prefers_hindi_english_mix(merchant, customer)
    if hindi:
        body = "Theek hai, samajh gayi — main isse aage badhati hoon aur update karti hoon."
    else:
        body = "Got it — I'll move ahead on that and update you shortly."
    return {
        "body": body,
        "cta": "open_ended",
        "rationale": f"Fallback template used (LLM unavailable: {error_note}); directive={directive}.",
    }
