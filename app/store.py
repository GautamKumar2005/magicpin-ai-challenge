"""In-memory state for the bot.

Everything here is process-local and non-persistent by design: the testing
brief requires the bot to keep state only for the duration of one test run,
and to wipe it on teardown. Run the app with a single worker process
(see Dockerfile / README) so all judge calls hit the same memory.
"""

import threading
import time
from typing import Any, Optional


class ContextStore:
    """Stores category / merchant / customer / trigger contexts.

    Keyed by (scope, context_id) -> {"version": int, "payload": dict}.
    Idempotent + monotonic per the /v1/context contract.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    def push(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, str, Optional[int]]:
        """Returns (accepted, reason, current_version_if_rejected)."""
        key = (scope, context_id)
        with self._lock:
            cur = self._data.get(key)
            if cur is not None and version <= cur["version"]:
                if version == cur["version"]:
                    return True, "noop_same_version", cur["version"]
                return False, "stale_version", cur["version"]
            self._data[key] = {"version": version, "payload": payload}
            return True, "stored", version

    def get(self, scope: str, context_id: str) -> Optional[dict]:
        entry = self._data.get((scope, context_id))
        return entry["payload"] if entry else None

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        entry = self._data.get((scope, context_id))
        return entry["version"] if entry else None

    def counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _cid) in self._data.keys():
            counts[scope] = counts.get(scope, 0) + 1
        return counts

    def all_ids(self, scope: str) -> list[str]:
        return [cid for (s, cid) in self._data.keys() if s == scope]

    def wipe(self) -> None:
        with self._lock:
            self._data.clear()


class ConversationStore:
    """Tracks in-flight conversations, per-merchant sent bodies, and
    suppression-key dedup so the bot doesn't repeat itself or re-fire a
    trigger it already acted on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.conversations: dict[str, dict[str, Any]] = {}
        self._used_suppression_keys: set[str] = set()
        self._merchant_messages: dict[str, list[str]] = {}
        self._merchant_autoreply_probed: set[str] = set()

    def suppression_key_used(self, key: str) -> bool:
        return bool(key) and key in self._used_suppression_keys

    def mark_suppression_key_used(self, key: str) -> None:
        if key:
            with self._lock:
                self._used_suppression_keys.add(key)

    def create(
        self,
        conversation_id: str,
        merchant_id: str,
        customer_id: Optional[str],
        category_slug: Optional[str],
        trigger_id: Optional[str],
    ) -> dict:
        conv = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "category_slug": category_slug,
            "trigger_id": trigger_id,
            "turns": [],  # [{"from": "vera"|"merchant"|"customer", "body": str, "ts": float}]
            "merchant_messages_normalized": [],  # for auto-reply detection
            "last_cta": None,
            "autoreply_probed": False,
            "ended": False,
            "sent_bodies": [],
            "created_at": time.time(),
        }
        with self._lock:
            self.conversations[conversation_id] = conv
        return conv

    def get(self, conversation_id: str) -> Optional[dict]:
        return self.conversations.get(conversation_id)

    def get_or_create(
        self,
        conversation_id: str,
        merchant_id: Optional[str],
        customer_id: Optional[str],
        category_slug: Optional[str],
    ) -> dict:
        conv = self.conversations.get(conversation_id)
        if conv is not None:
            return conv
        return self.create(conversation_id, merchant_id or "unknown", customer_id, category_slug, None)

    def record_bot_turn(self, conversation_id: str, body: str, cta: Optional[str]) -> None:
        conv = self.conversations[conversation_id]
        conv["turns"].append({"from": "vera", "body": body, "ts": time.time()})
        conv["sent_bodies"].append(body.strip().lower())
        conv["last_cta"] = cta

    def is_merchant_autoreply_probed(self, merchant_id: str) -> bool:
        with self._lock:
            return merchant_id in self._merchant_autoreply_probed

    def mark_merchant_autoreply_probed(self, merchant_id: str) -> None:
        if merchant_id:
            with self._lock:
                self._merchant_autoreply_probed.add(merchant_id)

    def record_incoming_turn(
        self,
        conversation_id: str,
        from_role: str,
        message: str,
        merchant_id: Optional[str] = None,
    ) -> int:
        """Returns how many times this exact message has appeared from the
        counterparty in this conversation or merchant history (used for auto-reply detection)."""
        conv = self.conversations[conversation_id]
        conv["turns"].append({"from": from_role, "body": message, "ts": time.time()})
        normalized = message.strip().lower()
        conv["merchant_messages_normalized"].append(normalized)
        count = conv["merchant_messages_normalized"].count(normalized)
        if merchant_id:
            with self._lock:
                m_list = self._merchant_messages.setdefault(merchant_id, [])
                m_list.append(normalized)
                count = max(count, m_list.count(normalized))
        return count

    def was_body_already_sent(self, conversation_id: str, body: str) -> bool:
        conv = self.conversations.get(conversation_id)
        if not conv:
            return False
        return body.strip().lower() in conv["sent_bodies"]

    def wipe(self) -> None:
        with self._lock:
            self.conversations.clear()
            self._used_suppression_keys.clear()
            self._merchant_messages.clear()
            self._merchant_autoreply_probed.clear()


context_store = ContextStore()
conversation_store = ConversationStore()
