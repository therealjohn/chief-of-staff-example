"""Async notification fan-out over a recipient repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from notifications import NotificationRecipient, RecipientGoneError


@dataclass
class FanOutResult:
    """Outcome counts for a single fan-out run."""

    sent: int
    failed: int
    pruned: int


async def async_notify_all(
    repository: Any,
    notification_text: str,
    send: Callable[[NotificationRecipient, str], Awaitable[None]],
) -> FanOutResult:
    """Send ``notification_text`` independently to every recipient in ``repository``."""
    sent = 0
    failed = 0
    pruned = 0

    for recipient in await repository.all():
        try:
            await send(recipient, notification_text)
            sent += 1
        except RecipientGoneError:
            await repository.remove(recipient.conversation_id)
            pruned += 1
        except Exception:
            failed += 1

    return FanOutResult(sent=sent, failed=failed, pruned=pruned)
