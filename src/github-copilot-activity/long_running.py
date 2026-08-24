"""Foundry-``@task``-shaped long-running turn handling.

A long-running Activity turn needs a way to (a) publish progress/text/done/
error events from the worker doing the actual work to the Activity handler
watching the same turn, (b) let the handler and the worker agree, exactly
once, on whether the turn is delivered by STREAMING back on the original
turn or DEFERRED (with a later proactive notification), and (c) drive that
agreement without any real sleeping.

This module is deliberately independent of the Foundry task SDK so it can be
unit tested with plain fakes. ``foundry_work.py`` is the thin ``@task`` adapter
that maps these interfaces to the real SDK.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, AsyncIterator, Awaitable, Callable

from pydantic import BaseModel, ConfigDict

DEFERRED_MESSAGE = "That'll take me a bit. I'll notify you when I'm done."


class DeliveryMode(Enum):
    STREAMING = auto()
    DEFERRED = auto()

PRE_TEXT_ERROR_MESSAGE = "Sorry, something went wrong while I was working on that."
POST_TEXT_ERROR_SUFFIX = " (Sorry, I hit an error and could not finish.)"

# Deadline, in seconds, used for each ``wait_for_event`` call made before any
# text has been delivered on the turn. No real sleeping occurs here -- the
# value is only ever handed to an injected waiter.
_WAIT_DEADLINE_SECONDS = 8.0


@dataclass
class WorkEvent:
    """A single progress/text/done/error event for one task's turn."""

    kind: str
    text: str | None = None


class WorkEventBroker:
    """Asyncio-queue-backed pub/sub for one task's events.

    Also owns the *stable* delivery-mode decision for the turn: the first
    call to :meth:`commit_delivery_mode` wins, and :meth:`delivery_mode`
    resolves once that decision has been made.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[WorkEvent] = asyncio.Queue()
        self._mode: DeliveryMode | None = None
        self._mode_decided = asyncio.Event()

    @property
    def mode(self) -> DeliveryMode | None:
        return self._mode

    async def publish(self, event: WorkEvent) -> None:
        await self._queue.put(event)

    async def next_event(self) -> WorkEvent:
        return await self._queue.get()

    def commit_delivery_mode(self, mode: DeliveryMode) -> DeliveryMode:
        """Record ``mode`` as the delivery decision if none has been made yet.

        Returns the (possibly already-committed) mode either way.
        """
        if self._mode is None:
            self._mode = mode
            self._mode_decided.set()
        return self._mode

    async def delivery_mode(self) -> DeliveryMode:
        await self._mode_decided.wait()
        assert self._mode is not None
        return self._mode


class DuplicateTaskError(Exception):
    """Raised by :meth:`BrokerRegistry.create` for an already-registered task id."""


class BrokerRegistry:
    """Maps task ids to :class:`WorkEventBroker` instances."""

    def __init__(self) -> None:
        self._brokers: dict[str, WorkEventBroker] = {}

    def create(self, task_id: str) -> WorkEventBroker:
        if task_id in self._brokers:
            raise DuplicateTaskError(task_id)
        broker = WorkEventBroker()
        self._brokers[task_id] = broker
        return broker

    def get(self, task_id: str) -> WorkEventBroker | None:
        return self._brokers.get(task_id)

    def remove(self, task_id: str) -> None:
        self._brokers.pop(task_id, None)


async def consume_activity_turn(
    broker: WorkEventBroker,
    stream: Any,
    wait_for_event: Callable[[float | None], Awaitable[WorkEvent | None]],
    on_cancel: Callable[[], Awaitable[None]],
    *,
    clock: Callable[[], float] = time.monotonic,
    first_text_deadline_seconds: float = _WAIT_DEADLINE_SECONDS,
    on_stream_complete: Callable[[], Awaitable[None]] | None = None,
    send_deferred: Callable[[str], Awaitable[None]] | None = None,
) -> DeliveryMode | None:
    """Drive one Activity turn from ``broker``'s events.

    See the module-level design notes (and the tests) for the exact
    progress/text/done/error/timeout/cancellation behavior.
    """
    text_started = False
    stream_started = False
    deadline = clock() + first_text_deadline_seconds

    while True:
        timeout = None if text_started else max(0.0, deadline - clock())
        event = await wait_for_event(timeout)

        if event is None:
            broker.commit_delivery_mode(DeliveryMode.DEFERRED)
            if send_deferred is not None and not stream_started:
                await send_deferred(DEFERRED_MESSAGE)
            else:
                stream.queue_text_chunk(DEFERRED_MESSAGE)
                await stream.end_stream()
            return broker.mode

        if event.kind == "progress":
            if not text_started:
                stream.queue_informative_update(event.text)
                stream_started = True
                if _stream_cancelled(stream):
                    await on_cancel()
                    await stream.end_stream()
                    return broker.mode
            continue

        if event.kind == "text":
            if not text_started:
                broker.commit_delivery_mode(DeliveryMode.STREAMING)
                text_started = True
                stream_started = True
            stream.queue_text_chunk(event.text)
            if _stream_cancelled(stream):
                await on_cancel()
                await stream.end_stream()
                return broker.mode
            continue

        if event.kind == "done":
            if on_stream_complete is not None:
                await on_stream_complete()
            await stream.end_stream()
            return broker.mode

        # event.kind == "error"
        if not text_started:
            broker.commit_delivery_mode(DeliveryMode.STREAMING)
            stream.queue_text_chunk(PRE_TEXT_ERROR_MESSAGE)
        else:
            stream.queue_text_chunk(POST_TEXT_ERROR_SUFFIX)
        await stream.end_stream()
        return broker.mode


def _stream_cancelled(stream: Any) -> bool:
    """Read the public test seam or the current SDK's private cancellation flag."""
    return bool(getattr(stream, "cancelled", getattr(stream, "_cancelled", False)))


class WorkInput(BaseModel):
    """Strict input schema for one long-running task.

    ``call_id`` is a top-level optional field needed by Foundry task
    recovery (it is not nested under any other field).
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    prompt: str
    files: list[dict[str, Any]] | None = None
    call_id: str | None = None


async def execute_work(
    ctx: Any,
    broker_registry: BrokerRegistry,
    ask_stream: Callable[[str, str, Any], AsyncIterator[tuple[str, str]]],
    send_proactive: Callable[[str, str], Awaitable[None]],
    abort: Callable[[str], Awaitable[None]],
) -> None:
    """Run the upstream work for one task.

    Forwards progress/text to the broker registered for ``ctx.task_id`` (if
    any) while collecting the complete text. At finish, delivers the result:
    a proactive notification when there is no broker
    (recovery) or the broker's mode is DEFERRED, or a ``done`` event when the
    broker's mode is STREAMING. If ``ctx.cancel`` becomes set, stops
    forwarding, awaits ``abort``, and publishes an ``error`` event instead of
    any success notification. The broker (if any) is always removed once the
    turn is terminal. Crash/retry/dedup handling is out of scope.
    """
    broker = broker_registry.get(ctx.task_id)
    conversation_id = ctx.input.conversation_id
    complete_text_parts: list[str] = []

    try:
        if ctx.cancel.is_set():
            await abort(conversation_id)
            if broker is not None:
                await broker.publish(WorkEvent(kind="error"))
            return

        cancelled = False
        async for kind, text in ask_stream(
            conversation_id, ctx.input.prompt, ctx.input.files
        ):
            if ctx.cancel.is_set():
                cancelled = True
                break

            if kind == "progress":
                if broker is not None:
                    await broker.publish(WorkEvent(kind="progress", text=text))
            else:  # "delta" or "final"
                complete_text_parts.append(text)
                if broker is not None:
                    await broker.publish(WorkEvent(kind="text", text=text))

        if ctx.cancel.is_set():
            cancelled = True

        if cancelled:
            await abort(conversation_id)
            if broker is not None:
                await broker.publish(WorkEvent(kind="error"))
            return

        complete_text = "".join(complete_text_parts)
        if broker is None:
            await send_proactive(conversation_id, complete_text)
            return

        mode = broker.mode or await broker.delivery_mode()
        if mode is DeliveryMode.DEFERRED:
            await send_proactive(conversation_id, complete_text)
        else:
            await broker.publish(WorkEvent(kind="done"))
    except Exception:
        if broker is not None:
            await broker.publish(WorkEvent(kind="error"))
        raise
    finally:
        broker_registry.remove(ctx.task_id)
