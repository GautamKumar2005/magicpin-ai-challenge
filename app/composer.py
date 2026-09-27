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
in India. You are composing ONE message. Follow these rules exactly:

1. Ground the message in a concrete, verifiable fact from the context you were given \
(a number, date, headline, or peer stat). Never write generic filler like "grow your \
business" or "increase your sales" with no anchor.
2. Match the category's voice and vocabulary exactly (see category.voice). Respect its \
taboos -- never use a taboo word or an overclaim.
3. Personalize to this specific merchant: use their real numbers, real offers, real \
signals. Do not invent anything not present in the context (no fake offers, no fake \
research citations, no fake competitor names, no fake numbers).
4. Prefer service+price framing ("Haircut @ 99") over generic percentage-off framing, \
when the category's offer_catalog has service+price options.
5. Exactly one call-to-action, and it must be the last sentence. Binary (yes/no style) \
for action triggers; no CTA for pure information triggers.
6. No preambles ("I hope you're doing well..."), no re-introducing yourself, be concise.
7. If the merchant/customer's language preference indicates Hindi, write in a natural \
Hindi-English code-mix (Hinglish), the way a peer would text -- not pure formal Hindi, \
not pure English.
8. Never repeat, verbatim or near-verbatim, any message already sent in this \
conversation (given to you as already_sent).
9. Use at least one compulsion lever: specificity, loss aversion, social proof, effort \
externalization, curiosity, reciprocity, asking the merchant a question, or a single \
binary commitment. Prefer social proof or "asking the merchant" when the data supports it \
-- these are the levers production Vera under-uses.

Respond with ONLY a JSON object, no markdown fences, no commentary, with exactly these \
keys: "body" (string), "cta" (one of "binary", "open_ended", "none"), "rationale" \
(one short sentence on why this message and what it should achieve)."""


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
