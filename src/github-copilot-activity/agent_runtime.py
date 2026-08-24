"""Thin orchestration layer between main.py and the domain/infra modules.

``RecipientService`` wraps proactive-recipient capture/lookup/broadcast
(``proactive_delivery`` + ``notification_delivery``) and ``WorkService``
wraps the one-active-task-per-conversation coordinator
(``task_coordinator.TaskCoordinator``) together with the per-task event
broker registry (``long_running.BrokerRegistry``) and the caller-supplied
``start_work``/``abort_work`` seams, so ``main.py`` only has to deal with a
small, focused API.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from notification_delivery import FanOutResult, async_notify_all
from notifications import NotificationRecipient
from proactive_delivery import recipient_from_turn_context

logger = logging.getLogger(__name__)

_REGISTER_ACTIONS = {"install", "message"}
_REMOVE_ACTIONS = {"uninstall"}
_PRESERVE_ACTIONS = {"add-upgrade", "remove-upgrade"}


class RecipientNotRegisteredError(Exception):
    """Raised by ``send_to_conversation`` when no recipient is registered."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"recipient not registered: {conversation_id}")
        self.conversation_id = conversation_id


class RecipientService:
    """Capture, look up and broadcast to registered proactive recipients."""

    def __init__(self, repository: Any, sender: Any) -> None:
        self._repository = repository
        self._sender = sender

    async def capture(self, context: Any, action: str) -> str:
        recipient = recipient_from_turn_context(context)
        if recipient is None:
            return "ignored"

        if action in _REGISTER_ACTIONS:
            created = await self._repository.upsert(recipient)
            return "registered" if created is not False else "refreshed"
        if action in _REMOVE_ACTIONS:
            await self._repository.remove(recipient.conversation_id)
            return "removed"
        if action in _PRESERVE_ACTIONS:
            return "preserved"
        raise ValueError(f"unsupported recipient action: {action}")

    async def send_to_conversation(self, conversation_id: str, text: str) -> None:
        recipient = await self._find_recipient(conversation_id)
        await self._sender.send(recipient, text)

    async def continue_conversation(
        self,
        conversation_id: str,
        callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        recipient = await self._find_recipient(conversation_id)
        await self._sender.continue_conversation(recipient, callback)

    async def broadcast(self, text: str) -> FanOutResult:
        return await async_notify_all(self._repository, text, self._sender.send)

    async def _find_recipient(self, conversation_id: str) -> NotificationRecipient:
        for recipient in await self._repository.all():
            if recipient.conversation_id == conversation_id:
                return recipient
        raise RecipientNotRegisteredError(conversation_id)


@dataclass
class WorkStart:
    """Outcome of a single :meth:`WorkService.start` call."""

    status: str  # "started" | "busy"
    task_id: str
    broker: Any | None


class WorkService:
    """Starts/cancels at most one active long-running task per conversation."""

    def __init__(
        self,
        task_coordinator: Any,
        broker_registry: Any,
        start_work: Callable[[str, Any], Awaitable[Any]],
        abort_work: Callable[[str], Awaitable[None]],
    ) -> None:
        self._coordinator = task_coordinator
        self._broker_registry = broker_registry
        self._start_work = start_work
        self._abort_work = abort_work
        self._runs: dict[str, Any] = {}
        self._watchers: dict[str, asyncio.Task] = {}

    async def start(self, conversation_id: str, work_input: Any) -> WorkStart:
        result = self._coordinator.start(conversation_id)
        if result.status == "busy":
            return WorkStart(status="busy", task_id=result.task_id, broker=None)

        task_id = result.task_id
        broker = self._broker_registry.create(task_id)
        try:
            run = await self._start_work(task_id, work_input)
        except Exception:
            self._coordinator.complete(conversation_id, task_id)
            self._broker_registry.remove(task_id)
            raise

        self._runs[task_id] = run
        watcher = asyncio.create_task(self._watch(conversation_id, task_id, run))
        self._watchers[task_id] = watcher
        return WorkStart(status="started", task_id=task_id, broker=broker)

    async def cancel(self, conversation_id: str) -> str:
        result = self._coordinator.cancel(conversation_id)
        if result.status == "none":
            return "none"

        task_id = result.task_id
        run = self._runs.pop(task_id, None)
        watcher = self._watchers.pop(task_id, None)
        self._broker_registry.remove(task_id)

        if run is not None:
            await run.cancel()
        await self._abort_work(conversation_id)
        if watcher is not None and not watcher.done():
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

        return task_id

    async def shutdown(self) -> None:
        watchers = list(self._watchers.values())
        for watcher in watchers:
            watcher.cancel()
        for watcher in watchers:
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("work watcher raised during shutdown")

    async def _watch(self, conversation_id: str, task_id: str, run: Any) -> None:
        try:
            await run.result()
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("work run %s failed", task_id)
        finally:
            self._coordinator.complete(conversation_id, task_id)
            self._broker_registry.remove(task_id)
            self._runs.pop(task_id, None)
            self._watchers.pop(task_id, None)
