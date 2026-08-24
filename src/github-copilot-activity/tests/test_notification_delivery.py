"""Tests for async notification fan-out over an async recipient repository
(item 2). Mirrors ``test_notification_fanout.py`` but for the async
``RecipientRepository`` seam (item 1): repository.all()/remove() are async,
and ``send`` is awaited.

Expected (not yet implemented) production module: ``notification_delivery``
  - ``async_notify_all(repository, notification_text, send) -> FanOutResult``
    where ``repository`` exposes async ``all()``/``remove(conversation_id)``
    and ``send(recipient, notification_text)`` is an async callable that may
    raise ``notifications.RecipientGoneError`` for a confirmed-stale recipient.
"""

from __future__ import annotations

import pytest

from notifications import NotificationRecipient, RecipientGoneError  # type: ignore[import-not-found]
from notification_delivery import async_notify_all  # type: ignore[import-not-found]


def _recipient(conversation_id: str) -> NotificationRecipient:
    return NotificationRecipient(
        conversation_id=conversation_id,
        is_personal=True,
        conversation_reference={"conversation": {"id": conversation_id}},
        claims={"tid": "tenant-1"},
    )


class _FakeAsyncRepository:
    """In-memory async double: no mock framework, just a dict + async methods."""

    def __init__(self, *recipients: NotificationRecipient) -> None:
        self._by_id = {r.conversation_id: r for r in recipients}

    async def all(self) -> list[NotificationRecipient]:
        return list(self._by_id.values())

    async def remove(self, conversation_id: str) -> None:
        self._by_id.pop(conversation_id, None)


@pytest.mark.asyncio
async def test_async_notify_all_sends_notification_text_to_every_recipient():
    repo = _FakeAsyncRepository(_recipient("conv-1"), _recipient("conv-2"))
    received: dict[str, str] = {}

    async def send(recipient, text):
        received[recipient.conversation_id] = text

    result = await async_notify_all(repo, "hello", send)

    assert received == {"conv-1": "hello", "conv-2": "hello"}
    assert (result.sent, result.failed, result.pruned) == (2, 0, 0)


@pytest.mark.asyncio
async def test_async_notify_all_continues_after_a_transient_failure_and_keeps_the_recipient():
    repo = _FakeAsyncRepository(_recipient("conv-1"), _recipient("conv-2"))

    async def flaky_send(recipient, text):
        if recipient.conversation_id == "conv-1":
            raise RuntimeError("transient network blip")

    result = await async_notify_all(repo, "hello", flaky_send)

    assert (result.sent, result.failed, result.pruned) == (1, 1, 0)
    remaining = {r.conversation_id for r in await repo.all()}
    assert remaining == {"conv-1", "conv-2"}


@pytest.mark.asyncio
async def test_async_notify_all_prunes_only_recipients_confirmed_gone():
    repo = _FakeAsyncRepository(_recipient("conv-1"), _recipient("conv-2"))

    async def gone_send(recipient, text):
        if recipient.conversation_id == "conv-1":
            raise RecipientGoneError(recipient.conversation_id)

    result = await async_notify_all(repo, "hello", gone_send)

    assert (result.sent, result.failed, result.pruned) == (1, 0, 1)
    remaining = {r.conversation_id for r in await repo.all()}
    assert remaining == {"conv-2"}


@pytest.mark.asyncio
async def test_async_notify_all_with_no_recipients_returns_zero_counts_and_never_calls_send():
    repo = _FakeAsyncRepository()
    calls = []

    async def send(recipient, text):
        calls.append(recipient)

    result = await async_notify_all(repo, "hello", send)

    assert calls == []
    assert (result.sent, result.failed, result.pruned) == (0, 0, 0)


@pytest.mark.asyncio
async def test_async_notify_all_reports_mixed_outcomes_in_one_run():
    repo = _FakeAsyncRepository(
        _recipient("gone"),
        _recipient("failed"),
        _recipient("sent"),
    )

    async def mixed_send(recipient, _text):
        if recipient.conversation_id == "gone":
            raise RecipientGoneError(recipient.conversation_id)
        if recipient.conversation_id == "failed":
            raise RuntimeError("temporary failure")

    result = await async_notify_all(repo, "hello", mixed_send)

    assert (result.sent, result.failed, result.pruned) == (1, 1, 1)
    assert {recipient.conversation_id for recipient in await repo.all()} == {
        "failed",
        "sent",
    }
