"""Async recipient repository backed by a FoundryStateStore-like store.

Wraps an injected async key/value store (matching the documented shape of
``azure.ai.agentserver.core.storage.FoundryStateStore``: ``set_item``,
``get_item``, ``delete_item``, ``list_keys``) to durably persist personal
Teams notification recipients (see ``notifications.NotificationRecipient``).

Deliberately dependency-free: no ``azure.*`` import here, only the documented
async method shapes are relied upon (duck typing / structural typing), so the
module stays easy to unit test against a plain in-memory double.
"""

from __future__ import annotations

import hashlib
from typing import Any

from azure.ai.agentserver.core.storage import FoundryStorageConflictError

from notifications import NotificationRecipient

_KEY_PREFIX = "teams-recipient/"
_TAGS = {"kind": "teams-recipient"}


def _key_for(conversation_id: str) -> str:
    """Deterministic, length-bounded (<=128 chars) key for a conversation id."""
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


def _serialize(recipient: NotificationRecipient) -> dict[str, Any]:
    return {
        "conversation_id": recipient.conversation_id,
        "is_personal": recipient.is_personal,
        "conversation_reference": recipient.conversation_reference,
        "claims": recipient.claims,
    }


def _deserialize(value: dict[str, Any]) -> NotificationRecipient:
    try:
        return NotificationRecipient(
            conversation_id=value["conversation_id"],
            is_personal=value["is_personal"],
            conversation_reference=value.get("conversation_reference", {}),
            claims=value.get("claims", {}),
        )
    except KeyError as exc:
        raise ValueError(f"corrupt stored recipient value: missing {exc.args[0]!r}") from exc


class RecipientRepository:
    """DI wrapper persisting :class:`NotificationRecipient` in a state store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    @classmethod
    async def open_foundry(cls, *, factory: Any = None) -> "RecipientRepository":
        """Open a repository backed by a Foundry-managed durable state store.

        ``factory`` is an async callable shaped like the production
        ``azure.ai.agentserver.core.storage.FoundryStateStore`` factory:
        ``await factory(name, *, user_isolation, item_ttl_seconds) -> store``.
        Defaults lazily to ``FoundryStateStore.get_or_create`` (imported only
        when actually needed) so this module stays import-safe for callers
        that always inject their own factory (e.g. tests).
        """
        if factory is None:
            from azure.ai.agentserver.core.storage import FoundryStateStore

            factory = FoundryStateStore.get_or_create

        store = await factory(
            "teams-notification-recipients",
            user_isolation=False,
            item_ttl_seconds=-1,
        )
        return cls(store)

    async def upsert(self, recipient: NotificationRecipient) -> bool:
        """Create or refresh a recipient, returning whether it was newly created."""
        key = _key_for(recipient.conversation_id)
        value = _serialize(recipient)
        try:
            await self._store.create_item(key, value, tags=_TAGS)
            return True
        except FoundryStorageConflictError:
            await self._store.set_item(key, value, tags=_TAGS)
            return False

    async def remove(self, conversation_id: str) -> None:
        await self._store.delete_item(_key_for(conversation_id))

    async def close(self) -> None:
        await self._store.aclose()

    async def all(self) -> list[NotificationRecipient]:
        recipients: list[NotificationRecipient] = []
        after: str | None = None
        while True:
            page = await self._store.list_keys(tags=_TAGS, after=after)
            for key in page.keys:
                item = await self._store.get_item(key.key)
                if item is None:
                    continue
                recipients.append(_deserialize(item.value))
            if not page.has_more:
                break
            after = page.last_id
        return recipients
