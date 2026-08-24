"""Tests for the async recipient repository backed by a FoundryStateStore-like
store (item 1: durable persistence for personal Teams notification recipients).

Expected (not yet implemented) production module: ``recipient_repository``
  - ``RecipientRepository(store)`` -- DI wrapper around an async store exposing
    ``set_item``, ``get_item``, ``delete_item``, ``list_keys`` matching
    ``azure.ai.agentserver.core.storage.FoundryStateStore``.
  - ``await upsert(recipient)`` / ``await remove(conversation_id)`` / ``await all()``.

The fake store below is a complete in-memory double of that documented shape
(page.keys / page.has_more / page.last_id; key objects with .key; items with
.value) -- no mock framework, no network.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from azure.ai.agentserver.core.storage import FoundryStorageConflictError

from notifications import NotificationRecipient  # type: ignore[import-not-found]
from recipient_repository import RecipientRepository  # type: ignore[import-not-found]

_TEAMS_RECIPIENT_TAGS = {"kind": "teams-recipient"}


@dataclass
class _FakeKey:
    key: str


@dataclass
class _FakeItem:
    value: dict


@dataclass
class _FakePage:
    keys: list
    has_more: bool
    last_id: str | None


class _FakeStateStore:
    """In-memory async double matching the documented FoundryStateStore shapes."""

    def __init__(self, *, page_size: int | None = None) -> None:
        self.items: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}
        self._page_size = page_size
        self.closed = False

    async def set_item(self, key: str, value: dict, *, tags: dict | None = None) -> None:
        self.items[key] = value
        self.tags[key] = dict(tags or {})

    async def create_item(self, key: str, value: dict, *, tags: dict | None = None) -> None:
        if key in self.items:
            raise FoundryStorageConflictError("item already exists", status_code=409)
        self.items[key] = value
        self.tags[key] = dict(tags or {})

    async def get_item(self, key: str):
        if key not in self.items:
            return None
        return _FakeItem(value=self.items[key])

    async def delete_item(self, key: str) -> None:
        self.items.pop(key, None)
        self.tags.pop(key, None)

    async def list_keys(
        self,
        *,
        tags: dict | None = None,
        limit: int | None = None,
        after: str | None = None,
        order: str = "desc",
    ) -> _FakePage:
        matching = sorted(
            k
            for k in self.items
            if tags is None or all(self.tags.get(k, {}).get(tk) == tv for tk, tv in tags.items())
        )
        if after is not None:
            idx = matching.index(after) + 1 if after in matching else len(matching)
            matching = matching[idx:]
        cap = limit if limit is not None else len(matching)
        if self._page_size is not None:
            cap = min(cap, self._page_size)
        page = matching[:cap]
        return _FakePage(
            keys=[_FakeKey(key=k) for k in page],
            has_more=len(matching) > len(page),
            last_id=page[-1] if page else None,
        )

    async def aclose(self) -> None:
        self.closed = True


def _recipient(conversation_id: str, tenant: str = "tenant-1") -> NotificationRecipient:
    return NotificationRecipient(
        conversation_id=conversation_id,
        is_personal=True,
        conversation_reference={"conversation": {"id": conversation_id}},
        claims={"tid": tenant},
    )


@pytest.mark.asyncio
async def test_upsert_persists_a_recipient_that_all_can_read_back():
    store = _FakeStateStore()
    repo = RecipientRepository(store)

    await repo.upsert(_recipient("conv-1"))

    assert await repo.all() == [_recipient("conv-1")]


@pytest.mark.asyncio
async def test_upsert_tags_the_stored_item_as_teams_recipient():
    store = _FakeStateStore()
    repo = RecipientRepository(store)

    await repo.upsert(_recipient("conv-1"))

    assert list(store.tags.values()) == [_TEAMS_RECIPIENT_TAGS]


@pytest.mark.asyncio
async def test_upsert_uses_a_key_no_longer_than_128_chars_even_for_long_ids():
    store = _FakeStateStore()
    repo = RecipientRepository(store)

    await repo.upsert(_recipient("c" * 300))

    assert store.items
    assert all(len(key) <= 128 for key in store.items)


@pytest.mark.asyncio
async def test_upsert_is_deterministic_and_replaces_rather_than_duplicates():
    store = _FakeStateStore()
    repo = RecipientRepository(store)

    created = await repo.upsert(_recipient("conv-1", tenant="tenant-old"))
    refreshed = await repo.upsert(_recipient("conv-1", tenant="tenant-new"))

    assert created is True
    assert refreshed is False
    assert len(store.items) == 1
    assert await repo.all() == [_recipient("conv-1", tenant="tenant-new")]


@pytest.mark.asyncio
async def test_remove_deletes_a_registered_recipient():
    store = _FakeStateStore()
    repo = RecipientRepository(store)
    await repo.upsert(_recipient("conv-1"))

    await repo.remove("conv-1")

    assert await repo.all() == []


@pytest.mark.asyncio
async def test_remove_is_idempotent_for_an_unknown_conversation_id():
    store = _FakeStateStore()
    repo = RecipientRepository(store)

    await repo.remove("never-registered")  # must not raise


@pytest.mark.asyncio
async def test_all_ignores_items_not_tagged_as_teams_recipient():
    store = _FakeStateStore()
    repo = RecipientRepository(store)
    await repo.upsert(_recipient("conv-1"))
    await store.set_item("unrelated/key", {"foo": "bar"}, tags={"kind": "something-else"})

    recipients = await repo.all()

    assert [r.conversation_id for r in recipients] == ["conv-1"]


@pytest.mark.asyncio
async def test_all_pages_through_multiple_list_keys_calls():
    store = _FakeStateStore(page_size=2)  # forces >1 page for 5 recipients
    repo = RecipientRepository(store)
    for i in range(5):
        await repo.upsert(_recipient(f"conv-{i}"))

    recipients = await repo.all()

    assert {r.conversation_id for r in recipients} == {f"conv-{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_all_raises_value_error_for_a_corrupt_stored_value():
    store = _FakeStateStore()
    repo = RecipientRepository(store)
    await store.set_item("teams-recipient/conv-1", {"not": "a recipient"}, tags=_TEAMS_RECIPIENT_TAGS)

    with pytest.raises(ValueError):
        await repo.all()


# ── RecipientRepository.open_foundry (Foundry-backed factory contract) ──────
#
# Expected (not yet implemented) production API:
#   ``await RecipientRepository.open_foundry(factory=...)`` where ``factory``
#   is an injected async callable shaped like the production
#   ``azure.ai.agentserver.core.storage`` Foundry state-store factory:
#   ``await factory(name, *, user_isolation, item_ttl_seconds) -> store``.
#   ``open_foundry`` calls it exactly once with the store name
#   ``"teams-notification-recipients"``, ``user_isolation=False`` (recipients
#   are shared across the whole tenant install, not per-user) and
#   ``item_ttl_seconds=-1`` (recipients never expire), and returns a
#   ``RecipientRepository`` wrapping the store the factory produced. This lets
#   the test prove the production factory contract without ever calling
#   Azure.


@pytest.mark.asyncio
async def test_open_foundry_calls_the_injected_factory_with_the_documented_store_contract():
    fake_store = _FakeStateStore()
    calls: list[dict] = []

    async def factory(name, *, user_isolation, item_ttl_seconds):
        calls.append(
            {"name": name, "user_isolation": user_isolation, "item_ttl_seconds": item_ttl_seconds}
        )
        return fake_store

    await RecipientRepository.open_foundry(factory=factory)

    assert calls == [
        {"name": "teams-notification-recipients", "user_isolation": False, "item_ttl_seconds": -1}
    ]


@pytest.mark.asyncio
async def test_open_foundry_calls_the_factory_exactly_once():
    fake_store = _FakeStateStore()
    call_count = 0

    async def factory(name, *, user_isolation, item_ttl_seconds):
        nonlocal call_count
        call_count += 1
        return fake_store

    await RecipientRepository.open_foundry(factory=factory)

    assert call_count == 1


@pytest.mark.asyncio
async def test_open_foundry_returns_a_repository_backed_by_the_factory_produced_store():
    fake_store = _FakeStateStore()

    async def factory(name, *, user_isolation, item_ttl_seconds):
        return fake_store

    repo = await RecipientRepository.open_foundry(factory=factory)
    await repo.upsert(_recipient("conv-1"))

    # Round-trips through the exact store instance the factory produced --
    # proves open_foundry actually wires up its result, not a new store.
    assert fake_store.items
    assert await repo.all() == [_recipient("conv-1")]


@pytest.mark.asyncio
async def test_close_closes_the_underlying_state_store():
    store = _FakeStateStore()
    repo = RecipientRepository(store)

    await repo.close()

    assert store.closed is True
