"""New coverage for a public ``ProactiveSender.continue_conversation`` method,
extending ``proactive_delivery.py`` (this is a *new* test file -- the existing
``tests/test_proactive_delivery.py`` is left untouched).

Expected (not yet implemented) production API:

  - ``await ProactiveSender.continue_conversation(recipient, callback)``
    reconstructs the real ``ConversationReference``/``ClaimsIdentity`` from
    ``recipient`` (exactly like ``.send`` does) and runs ``callback`` inside
    the adapter's continuation (``adapter.continue_conversation_with_claims``),
    handing the callback the live proactive ``TurnContext`` so upstream
    callers can deliver arbitrary activities (e.g. cards/files) after a
    deferred result, not just a text string. A confirmed write-blocked
    failure from the adapter is translated into
    ``notifications.RecipientGoneError``, same as ``.send``; other adapter
    failures propagate unchanged. ``.send`` remains behaviorally compatible
    (may delegate to ``continue_conversation`` internally) -- proven here by
    comparing the adapter calls each makes for an equivalent text send.
"""

from __future__ import annotations

import pytest

from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount
from microsoft_agents.hosting.core.app.proactive import Conversation
from microsoft_agents.hosting.core.authorization import ClaimsIdentity

from notifications import NotificationRecipient, RecipientGoneError  # type: ignore[import-not-found]
from proactive_delivery import ProactiveSender  # type: ignore[import-not-found]


def _activity(conversation_type: str, *, conversation_id: str = "conv-7") -> Activity:
    return Activity(
        type="message",
        channel_id="msteams",
        conversation=ConversationAccount(id=conversation_id, conversation_type=conversation_type),
        from_property=ChannelAccount(id="user-1"),
        recipient=ChannelAccount(id="bot-1"),
        service_url="https://smba.example.com/",
    )


class _FakeTurnContext:
    def __init__(self, activity: Activity, identity: ClaimsIdentity) -> None:
        self.activity = activity
        self.identity = identity


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
    """The turn context the fake adapter hands into the continuation callback."""

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
async def test_continue_conversation_runs_the_callback_with_a_live_proactive_turn_context():
    recipient = _stored_recipient()
    adapter = _FakeAdapter()
    received_texts: list[str] = []

    async def callback(turn_context):
        await turn_context.send_activity("proactive card follow-up")
        received_texts.append("callback ran")

    await ProactiveSender(adapter).continue_conversation(recipient, callback)

    assert received_texts == ["callback ran"]
    assert adapter.sent_texts == ["proactive card follow-up"]


@pytest.mark.asyncio
async def test_continue_conversation_reconstructs_identity_and_reference_from_the_recipient():
    recipient = _stored_recipient()
    adapter = _FakeAdapter()

    async def callback(turn_context):
        pass

    await ProactiveSender(adapter).continue_conversation(recipient, callback)

    assert adapter.calls == [
        {"claims": {"tid": "tenant-1"}, "conversation_id": "conv-7", "activity_type": "event"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _StatusError("Forbidden", 403),
        RuntimeError("MessageWritesBlocked: locked by tenant policy"),
    ],
)
async def test_continue_conversation_raises_recipient_gone_error_for_a_confirmed_write_block(error):
    recipient = _stored_recipient()
    adapter = _FakeAdapter(error=error)

    async def callback(turn_context):
        pass

    with pytest.raises(RecipientGoneError) as exc_info:
        await ProactiveSender(adapter).continue_conversation(recipient, callback)

    assert exc_info.value.conversation_id == "conv-7"


@pytest.mark.asyncio
async def test_continue_conversation_propagates_unrelated_adapter_failures():
    recipient = _stored_recipient()
    adapter = _FakeAdapter(error=RuntimeError("network unreachable"))

    async def callback(turn_context):
        pass

    with pytest.raises(RuntimeError):
        await ProactiveSender(adapter).continue_conversation(recipient, callback)


@pytest.mark.asyncio
async def test_continue_conversation_recovers_callback_errors_swallowed_by_the_adapter():
    recipient = _stored_recipient()
    blocked = _StatusError("Forbidden", 403)

    class SwallowingAdapter(_FakeAdapter):
        async def continue_conversation_with_claims(
            self,
            claims_identity,
            continuation_activity,
            callback,
            audience=None,
        ):
            try:
                await callback(_FakeProactiveTurnContext(self))
            except Exception:
                return

    async def callback(_turn_context):
        raise blocked

    with pytest.raises(RecipientGoneError):
        await ProactiveSender(SwallowingAdapter()).continue_conversation(
            recipient,
            callback,
        )


@pytest.mark.asyncio
async def test_send_and_continue_conversation_issue_equivalent_adapter_calls_for_an_equivalent_text_send():
    recipient = _stored_recipient()
    adapter_via_send = _FakeAdapter()
    adapter_via_continue = _FakeAdapter()

    await ProactiveSender(adapter_via_send).send(recipient, "hello there")

    async def callback(turn_context):
        await turn_context.send_activity("hello there")

    await ProactiveSender(adapter_via_continue).continue_conversation(recipient, callback)

    assert adapter_via_send.calls == adapter_via_continue.calls
    assert adapter_via_send.sent_texts == adapter_via_continue.sent_texts
