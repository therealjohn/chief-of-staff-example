"""Tests for proactive delivery to personal Teams conversations
(proactive_delivery). Uses the real M365 ``Conversation.from_turn_context``
serialization, real ``microsoft_agents.activity`` domain types and a real
``ClaimsIdentity`` for test data; only the turn context / adapter at the
external boundary are tiny fakes. No network.

Expected (not yet implemented) production module: ``proactive_delivery``

  - ``recipient_from_turn_context(context) -> NotificationRecipient | None``
    -- built on top of
    ``microsoft_agents.hosting.core.app.proactive.Conversation.from_turn_context``.
    Returns ``None`` when the turn's conversation is not personal
    (``activity.conversation.conversation_type != "personal"``). Otherwise
    returns a ``notifications.NotificationRecipient`` capturing:
      * ``conversation_id`` -- the conversation's id,
      * ``conversation_reference`` -- the *full* serialized
        ``ConversationReference`` (as produced by
        ``Conversation.store_item_to_json()["conversation_reference"]``),
      * ``claims`` -- the filtered proactive claims (as produced by
        ``Conversation.store_item_to_json()["claims"]``).

  - ``ProactiveSender(adapter)`` with ``await .send(recipient, text)`` that:
      1. reconstructs a real ``ConversationReference`` from
         ``recipient.conversation_reference`` and a real ``ClaimsIdentity``
         from ``recipient.claims``,
      2. calls
         ``await adapter.continue_conversation_with_claims(claims_identity,
         continuation_activity, callback)`` (the ``ChannelAdapter``-shaped
         seam), where ``continuation_activity`` is the reconstructed
         reference's ``get_continuation_activity()``,
      3. sends ``text`` from *inside* the continuation ``callback`` (i.e.
         via the ``TurnContext`` handed to the callback, not before/after
         the adapter call).

    A confirmed write-blocked failure from the adapter (a 403 /
    ``MessageWritesBlocked``-shaped error) is translated into
    ``notifications.RecipientGoneError``; any other adapter failure
    propagates unchanged.
"""

from __future__ import annotations

import pytest

from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount
from microsoft_agents.hosting.core.app.proactive import Conversation
from microsoft_agents.hosting.core.authorization import ClaimsIdentity

from notifications import NotificationRecipient, RecipientGoneError  # type: ignore[import-not-found]

from proactive_delivery import ProactiveSender, recipient_from_turn_context  # type: ignore[import-not-found]


def _activity(conversation_type: str, *, conversation_id: str = "conv-1") -> Activity:
    return Activity(
        type="message",
        channel_id="msteams",
        conversation=ConversationAccount(id=conversation_id, conversation_type=conversation_type),
        from_property=ChannelAccount(id="user-1"),
        recipient=ChannelAccount(id="bot-1"),
        service_url="https://smba.example.com/",
    )


class _FakeTurnContext:
    """Tiny fake at the external boundary: only what Conversation.from_turn_context reads."""

    def __init__(self, activity: Activity, identity: ClaimsIdentity | None) -> None:
        self.activity = activity
        self.identity = identity


# ── recipient_from_turn_context ─────────────────────────────────────────────


def test_recipient_from_turn_context_returns_none_for_a_non_personal_conversation():
    context = _FakeTurnContext(_activity("channel"), ClaimsIdentity(claims={"tid": "tenant-1"}))

    assert recipient_from_turn_context(context) is None


def test_recipient_from_turn_context_captures_the_conversation_id_for_a_personal_conversation():
    context = _FakeTurnContext(
        _activity("personal", conversation_id="conv-42"), ClaimsIdentity(claims={"tid": "tenant-1"})
    )

    recipient = recipient_from_turn_context(context)

    assert recipient is not None
    assert recipient.conversation_id == "conv-42"
    assert recipient.is_personal is True


def test_recipient_from_turn_context_captures_the_full_serialized_conversation_reference():
    activity = _activity("personal")
    identity = ClaimsIdentity(claims={"tid": "tenant-1"})
    context = _FakeTurnContext(activity, identity)
    expected_reference = Conversation.from_turn_context(context).store_item_to_json()["conversation_reference"]

    recipient = recipient_from_turn_context(context)

    assert recipient.conversation_reference == expected_reference


def test_recipient_from_turn_context_filters_claims_to_the_persisted_proactive_claim_keys():
    activity = _activity("personal")
    identity = ClaimsIdentity(claims={"tid": "tenant-1", "aud": "aud-1", "unwanted": "drop-me"})
    context = _FakeTurnContext(activity, identity)

    recipient = recipient_from_turn_context(context)

    assert recipient.claims == {"tid": "tenant-1", "aud": "aud-1"}


# ── ProactiveSender ──────────────────────────────────────────────────────────


def _stored_recipient(conversation_id: str = "conv-7") -> NotificationRecipient:
    activity = _activity("personal", conversation_id=conversation_id)
    identity = ClaimsIdentity(claims={"tid": "tenant-1"})
    context = _FakeTurnContext(activity, identity)
    serialized = Conversation.from_turn_context(context).store_item_to_json()
    return NotificationRecipient(
        conversation_id=conversation_id,
        is_personal=True,
        conversation_reference=serialized["conversation_reference"],
        claims=serialized["claims"],
    )


class _FakeProactiveTurnContext:
    """Tiny fake handed to the continuation callback by the fake adapter below."""

    def __init__(self, adapter: "_FakeAdapter") -> None:
        self._adapter = adapter

    async def send_activity(self, text: str) -> None:
        self._adapter.sent_texts.append(text)


class _FakeAdapter:
    """Complete async double for the ``continue_conversation_with_claims`` seam."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.sent_texts: list[str] = []
        self._error = error

    async def continue_conversation_with_claims(self, claims_identity, continuation_activity, callback, audience=None):
        self.calls.append(
            {
                "claims": dict(claims_identity.claims),
                "conversation_id": (
                    continuation_activity.conversation.id if continuation_activity.conversation else None
                ),
                "activity_type": continuation_activity.type,
            }
        )
        if self._error is not None:
            raise self._error
        await callback(_FakeProactiveTurnContext(self))


class _StatusError(RuntimeError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@pytest.mark.asyncio
async def test_proactive_sender_send_continues_the_conversation_using_the_recipients_claims_and_reference():
    recipient = _stored_recipient()
    adapter = _FakeAdapter()

    await ProactiveSender(adapter).send(recipient, "hello there")

    assert adapter.calls == [
        {"claims": {"tid": "tenant-1"}, "conversation_id": "conv-7", "activity_type": "event"}
    ]


@pytest.mark.asyncio
async def test_proactive_sender_send_sends_the_text_inside_the_continuation_callback():
    recipient = _stored_recipient()
    adapter = _FakeAdapter()

    await ProactiveSender(adapter).send(recipient, "hello there")

    assert adapter.sent_texts == ["hello there"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _StatusError("Forbidden", 403),
        RuntimeError("MessageWritesBlocked: locked by tenant policy"),
    ],
)
async def test_proactive_sender_send_raises_recipient_gone_error_for_a_confirmed_write_block(error):
    recipient = _stored_recipient()
    adapter = _FakeAdapter(error=error)

    with pytest.raises(RecipientGoneError) as exc_info:
        await ProactiveSender(adapter).send(recipient, "hello there")

    assert exc_info.value.conversation_id == "conv-7"


@pytest.mark.asyncio
async def test_proactive_sender_does_not_prune_for_unrelated_error_text_containing_403():
    recipient = _stored_recipient("conversation-403")
    error = _StatusError("500 failure for conversation-403", 500)
    adapter = _FakeAdapter(error=error)

    with pytest.raises(_StatusError) as exc_info:
        await ProactiveSender(adapter).send(recipient, "hello there")

    assert exc_info.value is error


@pytest.mark.asyncio
async def test_proactive_sender_send_propagates_unrelated_adapter_failures():
    recipient = _stored_recipient()
    adapter = _FakeAdapter(error=RuntimeError("network unreachable"))

    with pytest.raises(RuntimeError):
        await ProactiveSender(adapter).send(recipient, "hello there")
