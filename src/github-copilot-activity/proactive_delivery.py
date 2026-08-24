"""Proactive delivery to personal Teams conversations.

Captures the durable recipient shape (``notifications.NotificationRecipient``)
from a turn context, and sends proactive messages back into that
conversation using the real M365 ``ConversationReference`` / ``ClaimsIdentity``
reconstruction path plus the ``ChannelAdapter.continue_conversation_with_claims``
seam.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from microsoft_agents.activity import ConversationReference
from microsoft_agents.hosting.core.app.proactive import Conversation

from notifications import NotificationRecipient, RecipientGoneError

def recipient_from_turn_context(context: Any) -> NotificationRecipient | None:
    """Build a :class:`NotificationRecipient` from the current turn context.

    Returns ``None`` when the turn's conversation is not personal.
    """
    activity = context.activity
    conversation = activity.conversation
    if conversation is None or conversation.conversation_type != "personal":
        return None

    stored = Conversation.from_turn_context(context).store_item_to_json()
    return NotificationRecipient(
        conversation_id=conversation.id,
        is_personal=True,
        conversation_reference=stored["conversation_reference"],
        claims=stored["claims"],
    )


def _is_write_blocked(error: Exception) -> bool:
    status = getattr(error, "status", getattr(error, "status_code", None))
    if status == 403:
        return True
    return "messagewritesblocked" in str(error).lower()


class ProactiveSender:
    """Sends proactive text messages to a previously-captured recipient."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def continue_conversation(
        self, recipient: NotificationRecipient, callback: Callable[[Any], Awaitable[None]]
    ) -> None:
        """Reconstruct the recipient's conversation and run ``callback`` inside it.

        ``callback`` is handed the live proactive ``TurnContext`` so callers
        can deliver arbitrary activities (not just a text string).
        """
        reference = ConversationReference.model_validate(recipient.conversation_reference)
        claims_identity = Conversation.identity_from_claims(recipient.claims)
        continuation_activity = reference.get_continuation_activity()
        captured_error: BaseException | None = None

        async def _guarded_callback(turn_context: Any) -> None:
            nonlocal captured_error
            try:
                await callback(turn_context)
            except BaseException as exc:
                captured_error = exc

        try:
            await self._adapter.continue_conversation_with_claims(
                claims_identity, continuation_activity, _guarded_callback
            )
        except Exception as exc:
            if _is_write_blocked(exc):
                raise RecipientGoneError(recipient.conversation_id) from exc
            raise

        if captured_error is not None:
            if isinstance(captured_error, Exception) and _is_write_blocked(captured_error):
                raise RecipientGoneError(recipient.conversation_id) from captured_error
            raise captured_error

    async def send(self, recipient: NotificationRecipient, text: str) -> None:
        async def _callback(turn_context: Any) -> None:
            await turn_context.send_activity(text)

        await self.continue_conversation(recipient, _callback)
