"""Deterministic heuristics that drive the /v1/reply state machine.

These run *before* any LLM call so that auto-reply handling, intent
handoffs and hard declines are fast, cheap and 100% reproducible -- exactly
the failure modes the brief calls out in today's production Vera.
"""

import re

AUTOREPLY_KEYWORDS = [
    "thank you for contacting",
    "thanks for reaching out",
    "we will get back to you",
    "we'll get back to you",
    "automated assistant",
    "automated message",
    "hamari team tak pahuncha",
    "team ko forward",
    "aapki jaankari ke liye",
    "shukriya",  # weak signal on its own, combined with other cues below
    "this is an auto",
    "out of office",
    "busy at the moment and will respond",
]

DECLINE_KEYWORDS = [
    "not interested",
    "no thanks",
    "no thank you",
    "stop",
    "unsubscribe",
    "nahi chahiye",
    "interested nahi",
    "band karo",
    "mat bhejo",
    "leave me alone",
    "don't message",
    "do not message",
]

WAIT_KEYWORDS = [
    "call you later",
    "call me later",
    "abhi busy",
    "busy hoon",
    "not now",
    "abhi nahi",
    "thodi der baad",
    "later please",
    "will check later",
    "let me check and get back",
    "give me some time",
]

INTENT_CONFIRM_KEYWORDS = [
    "yes let's do it",
    "let's do it",
    "lets do it",
    "go ahead",
    "ok let's do it",
    "ok lets do it",
    "sounds good let's",
    "chalo",
    "haan karo",
    "karo",
    "start karo",
    "join karna hai",
    "i want to join",
    "sign me up",
    "yes please do it",
    "yes send it",
    "yes send",
    "please proceed",
    "proceed",
    "sure go ahead",
    "kar do",
    "kar dijiye",
    "haan bhejo",
]

HOSTILE_KEYWORDS = [
    "useless",
    "stupid bot",
    "shut up",
    "scam",
    "fraud",
    "bakwas",
    "bewakoof",
    "spam mat karo",
]

HINDI_HINT_PATTERN = re.compile(
    r"\b(hai|hoon|kar|karo|kya|nahi|haan|aap|apka|apke|bhejo|chalo|kijiye|dijiye)\b",
    re.IGNORECASE,
)


def _contains_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def looks_like_autoreply_text(message: str) -> bool:
    return _contains_any(message, AUTOREPLY_KEYWORDS)


def looks_like_decline(message: str) -> bool:
    return _contains_any(message, DECLINE_KEYWORDS)


def looks_like_wait_request(message: str) -> bool:
    return _contains_any(message, WAIT_KEYWORDS)


def looks_like_intent_confirmation(message: str) -> bool:
    return _contains_any(message, INTENT_CONFIRM_KEYWORDS)


def looks_hostile(message: str) -> bool:
    return _contains_any(message, HOSTILE_KEYWORDS)


def looks_hindi_mixed(text: str) -> bool:
    return bool(HINDI_HINT_PATTERN.search(text))


def prefers_hindi_english_mix(merchant: dict | None, customer: dict | None) -> bool:
    """Decide whether the merchant/customer's language preference calls for
    Hindi-English code-mix, per identity.languages / language_pref."""
    if customer:
        pref = (customer.get("identity", {}) or {}).get("language_pref", "")
        if "hi" in pref.lower():
            return True
    if merchant:
        langs = (merchant.get("identity", {}) or {}).get("languages", []) or []
        if any("hi" == str(lang).lower() for lang in langs):
            return True
    return False


def classify_incoming_message(
    message: str,
    repeat_count: int,
) -> str:
    """Returns one of: 'autoreply_confirmed', 'autoreply_probe',
    'decline', 'wait', 'intent_confirm', 'hostile', 'normal'.

    repeat_count: how many times this exact normalized message has been
    seen from this counterparty in this conversation (including the
    current one).
    """
    if repeat_count >= 3:
        return "autoreply_confirmed"
    if looks_like_autoreply_text(message) or repeat_count == 2:
        return "autoreply_probe"
    if looks_like_decline(message):
        return "decline"
    if looks_hostile(message):
        return "hostile"
    if looks_like_intent_confirmation(message):
        return "intent_confirm"
    if looks_like_wait_request(message):
        return "wait"
    return "normal"
