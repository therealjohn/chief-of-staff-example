"""Personal Teams notification recipient types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NotificationRecipient:
    """A single registered proactive-notification recipient."""

    conversation_id: str
    is_personal: bool
    conversation_reference: dict[str, Any] = field(default_factory=dict)
    claims: dict[str, Any] = field(default_factory=dict)


class RecipientGoneError(Exception):
    """Raised by a caller-supplied ``send`` to report a confirmed-stale recipient."""

    def __init__(self, conversation_id: str):
        super().__init__(f"recipient gone: {conversation_id}")
        self.conversation_id = conversation_id
