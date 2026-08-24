"""Tests for Foundry ``@task``-shaped long-running turn handling.

Design ref (item 7): a long-running Activity turn needs a way to (a) publish
progress/text/done/error events from the worker doing the actual work to the
Activity handler watching the same turn, (b) let the handler and the worker
agree, exactly once, on whether the turn is delivered by STREAMING back on
the original turn or DEFERRED (with a later proactive notification), and (c)
drive that agreement without any real sleeping -- an injected async
``wait_for_event(timeout_or_none)`` stands in for the real "next event or
deadline" wait.

No Azure / Foundry SDK types are used anywhere in these tests: only plain
dataclasses, an ``asyncio.Queue``-backed broker, and small hand-written async
fakes for the stream, the waiter, the turn context, and the upstream
``ask_stream``-shaped async generator.

Expected (not yet implemented) production module: ``long_running``

  - ``WorkEvent(kind: str, text: str | None = None)`` -- ``kind`` is one of
    ``"progress"`` / ``"text"`` / ``"done"`` / ``"error"``.

  - ``WorkEventBroker()`` -- asyncio-queue-backed pub/sub for one task's
    events, plus a *stable* delivery-mode decision:
      - ``await .publish(event)`` / ``await .next_event() -> WorkEvent``
      - ``.mode -> DeliveryMode | None`` (undecided until committed)
      - ``.commit_delivery_mode(mode) -> DeliveryMode`` -- first call wins;
        later calls with a different mode return the already-committed one
        and do not change ``.mode``.
      - ``await .delivery_mode() -> DeliveryMode`` -- resolves once a mode
        has been committed (a worker awaits this after the Activity handler
        decides).

  - ``BrokerRegistry()`` -- maps task ids to brokers:
      - ``.create(task_id) -> WorkEventBroker`` -- raises
        ``DuplicateTaskError`` for an already-registered ``task_id``.
      - ``.get(task_id) -> WorkEventBroker | None``
      - ``.remove(task_id) -> None`` -- idempotent.

  - ``async consume_activity_turn(broker, stream, wait_for_event, on_cancel)
    -> DeliveryMode | None`` -- drives one Activity turn from the broker's
    events:
      - before any text: calls ``wait_for_event(<finite remaining seconds>)``;
        a ``progress`` event is forwarded via
        ``stream.queue_informative_update`` and leaves the mode undecided; a
        timeout (``wait_for_event`` returning ``None``) commits
        ``DeliveryMode.DEFERRED``, queues the exact
        ``delivery_mode.DEFERRED_MESSAGE`` as text, ends the stream, and
        returns ``DeliveryMode.DEFERRED``.
      - the first ``text`` event commits ``DeliveryMode.STREAMING``, forwards
        the text via ``stream.queue_text_chunk``, and switches subsequent
        waits to ``wait_for_event(None)`` (no deadline).
      - after the first text: later ``progress`` events are ignored, later
        ``text`` events are forwarded, and a ``done`` event ends the stream
        and returns ``DeliveryMode.STREAMING``.
      - an ``error`` event before any text is streamed as
        ``PRE_TEXT_ERROR_MESSAGE``, commits ``DeliveryMode.STREAMING`` (it was
        already delivered on this turn), and ends the stream; an ``error``
        event after text appends ``POST_TEXT_ERROR_SUFFIX`` and ends the
        stream.
      - if ``stream.cancelled`` becomes true (checked right after a send),
        ``on_cancel`` is awaited and the stream is ended; the turn returns
        whatever mode (if any) had been committed so far.

  - ``WorkInput`` -- a strict (``extra="forbid"``) pydantic model with
    ``conversation_id: str``, ``prompt: str``, optional ``files``, and a
    top-level optional ``call_id`` (needed by Foundry task recovery).

  - ``async execute_work(ctx, broker_registry, ask_stream, send_proactive,
    abort) -> None`` -- ``ctx`` has ``.task_id``, ``.input`` (a
    ``WorkInput``), ``.cancel`` (an ``asyncio.Event``); ``ask_stream`` is an
    ``ask_stream``-shaped async generator over existing upstream
    ``(kind, text)`` pairs (``"progress"`` / ``"delta"`` / ``"final"``).
    Collects the complete text while forwarding progress/text to the broker
    registered for ``ctx.task_id`` (if any). At finish: no broker (recovery)
    or a ``DEFERRED`` broker mode calls ``send_proactive(conversation_id,
    complete_text)``; a ``STREAMING`` broker mode instead publishes a
    ``done`` event (no proactive duplicate). If ``ctx.cancel`` becomes set,
    ``abort(conversation_id)`` is awaited, an ``error`` event is published
    instead of a success notification, and no proactive send happens. The
    broker (if any) is always removed for ``ctx.task_id`` once the turn is
    terminal. Crash/retry/dedup handling is out of scope.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from long_running import (  # type: ignore[import-not-found]
    DEFERRED_MESSAGE,
    POST_TEXT_ERROR_SUFFIX,
    PRE_TEXT_ERROR_MESSAGE,
    BrokerRegistry,
    DeliveryMode,
    DuplicateTaskError,
    WorkEvent,
    WorkEventBroker,
    WorkInput,
    consume_activity_turn,
    execute_work,
)


async def _noop_cancel() -> None:
    return None


class ScriptedWaiter:
    """A canned ``wait_for_event`` async callable; records every timeout arg."""

    def __init__(self, script: list[WorkEvent | None]) -> None:
        self._script = list(script)
        self.timeouts: list[float | None] = []

    async def __call__(self, timeout: float | None) -> WorkEvent | None:
        self.timeouts.append(timeout)
        return self._script.pop(0)


class FakeStream:
    """A complete, tiny stand-in for the Activity streaming-response object."""

    def __init__(self, cancel_after: str | None = None) -> None:
        self.informative_updates: list[str] = []
        self.text_chunks: list[str] = []
        self.ended = False
        self.cancelled = False
        self._cancel_after = cancel_after

    def queue_informative_update(self, text: str) -> None:
        self.informative_updates.append(text)
        if self._cancel_after == "progress":
            self.cancelled = True

    def queue_text_chunk(self, text: str) -> None:
        self.text_chunks.append(text)
        if self._cancel_after == "text":
            self.cancelled = True

    async def end_stream(self) -> None:
        self.ended = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeCtx:
    task_id: str
    input: WorkInput
    cancel: asyncio.Event


# ── WorkEvent ────────────────────────────────────────────────────────────────


def test_work_event_holds_kind_and_text():
    event = WorkEvent(kind="text", text="hello")

    assert event.kind == "text"
    assert event.text == "hello"


def test_work_event_text_defaults_to_none():
    event = WorkEvent(kind="done")

    assert event.text is None


# ── WorkEventBroker ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broker_next_event_returns_published_events_in_publish_order():
    broker = WorkEventBroker()
    await broker.publish(WorkEvent(kind="progress", text="step 1"))
    await broker.publish(WorkEvent(kind="text", text="hello"))

    first = await broker.next_event()
    second = await broker.next_event()

    assert first == WorkEvent(kind="progress", text="step 1")
    assert second == WorkEvent(kind="text", text="hello")


def test_broker_mode_is_undecided_until_committed():
    broker = WorkEventBroker()

    assert broker.mode is None


def test_broker_commit_delivery_mode_records_the_first_decision():
    broker = WorkEventBroker()

    committed = broker.commit_delivery_mode(DeliveryMode.STREAMING)

    assert committed is DeliveryMode.STREAMING
    assert broker.mode is DeliveryMode.STREAMING


def test_broker_recommitting_a_different_mode_does_not_change_the_first_decision():
    broker = WorkEventBroker()
    broker.commit_delivery_mode(DeliveryMode.STREAMING)

    second = broker.commit_delivery_mode(DeliveryMode.DEFERRED)

    assert second is DeliveryMode.STREAMING
    assert broker.mode is DeliveryMode.STREAMING


@pytest.mark.asyncio
async def test_broker_delivery_mode_await_returns_the_already_committed_mode():
    broker = WorkEventBroker()
    broker.commit_delivery_mode(DeliveryMode.DEFERRED)

    mode = await broker.delivery_mode()

    assert mode is DeliveryMode.DEFERRED


# ── BrokerRegistry ───────────────────────────────────────────────────────────


def test_registry_create_returns_a_new_broker_for_a_task_id():
    registry = BrokerRegistry()

    broker = registry.create("task-1")

    assert isinstance(broker, WorkEventBroker)


def test_registry_get_returns_the_broker_created_for_a_task_id():
    registry = BrokerRegistry()
    created = registry.create("task-1")

    assert registry.get("task-1") is created


def test_registry_get_returns_none_for_an_unknown_task_id():
    registry = BrokerRegistry()

    assert registry.get("never-created") is None


def test_registry_create_rejects_a_duplicate_task_id():
    registry = BrokerRegistry()
    registry.create("task-1")

    with pytest.raises(DuplicateTaskError):
        registry.create("task-1")


def test_registry_remove_drops_a_registered_broker():
    registry = BrokerRegistry()
    registry.create("task-1")

    registry.remove("task-1")

    assert registry.get("task-1") is None


def test_registry_remove_is_idempotent_for_an_unknown_task_id():
    registry = BrokerRegistry()

    registry.remove("never-created")  # must not raise


# ── consume_activity_turn ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_progress_before_text_is_forwarded_as_an_informative_update():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([WorkEvent(kind="progress", text="Looking things up..."), None])

    await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert stream.informative_updates == ["Looking things up..."]


@pytest.mark.asyncio
async def test_progress_before_text_keeps_the_deadline_finite_on_the_next_wait():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([WorkEvent(kind="progress", text="still working"), None])

    await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert waiter.timeouts[0] is not None
    assert waiter.timeouts[1] is not None  # still undecided -> still deadline-bound


@pytest.mark.asyncio
async def test_progress_does_not_reset_the_absolute_first_text_deadline():
    broker = WorkEventBroker()
    stream = FakeStream()
    clock = FakeClock()
    timeouts: list[float | None] = []
    events = [WorkEvent(kind="progress", text="still working"), None]

    async def wait_for_event(timeout):
        timeouts.append(timeout)
        clock.advance(3.0)
        return events.pop(0)

    await consume_activity_turn(
        broker,
        stream,
        wait_for_event,
        _noop_cancel,
        clock=clock,
        first_text_deadline_seconds=8.0,
    )

    assert timeouts == [8.0, 5.0]


@pytest.mark.asyncio
async def test_timeout_before_any_text_commits_deferred_and_queues_the_exact_deferred_message():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([None])

    mode = await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert mode is DeliveryMode.DEFERRED
    assert stream.text_chunks == [DEFERRED_MESSAGE]
    assert stream.ended is True
    assert broker.mode is DeliveryMode.DEFERRED


@pytest.mark.asyncio
async def test_timeout_before_stream_start_can_send_one_regular_deferred_reply():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([None])
    sent: list[str] = []

    async def send_deferred(text: str) -> None:
        sent.append(text)

    mode = await consume_activity_turn(
        broker,
        stream,
        waiter,
        _noop_cancel,
        send_deferred=send_deferred,
    )

    assert mode is DeliveryMode.DEFERRED
    assert sent == [DEFERRED_MESSAGE]
    assert stream.text_chunks == []
    assert stream.ended is False


@pytest.mark.asyncio
async def test_first_text_commits_streaming_and_is_forwarded_immediately():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([WorkEvent(kind="text", text="Here is the answer."), WorkEvent(kind="done")])

    mode = await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert mode is DeliveryMode.STREAMING
    assert stream.text_chunks == ["Here is the answer."]
    assert broker.mode is DeliveryMode.STREAMING


@pytest.mark.asyncio
async def test_after_first_text_wait_for_event_is_called_without_a_deadline():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([WorkEvent(kind="text", text="Hello"), WorkEvent(kind="done")])

    await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert waiter.timeouts[0] is not None
    assert waiter.timeouts[1] is None


@pytest.mark.asyncio
async def test_progress_after_first_text_is_ignored_but_later_text_is_still_forwarded():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter(
        [
            WorkEvent(kind="text", text="Part one. "),
            WorkEvent(kind="progress", text="still working"),
            WorkEvent(kind="text", text="Part two."),
            WorkEvent(kind="done"),
        ]
    )

    mode = await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert stream.informative_updates == []
    assert stream.text_chunks == ["Part one. ", "Part two."]
    assert mode is DeliveryMode.STREAMING


@pytest.mark.asyncio
async def test_done_after_text_ends_the_stream_and_returns_streaming():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([WorkEvent(kind="text", text="Hello"), WorkEvent(kind="done")])

    mode = await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert stream.ended is True
    assert mode is DeliveryMode.STREAMING


@pytest.mark.asyncio
async def test_stream_completion_callback_runs_before_the_final_stream_message():
    broker = WorkEventBroker()
    order: list[str] = []

    class OrderedStream(FakeStream):
        async def end_stream(self) -> None:
            order.append("end")
            await super().end_stream()

    async def on_stream_complete() -> None:
        order.append("attachments")

    await consume_activity_turn(
        broker,
        OrderedStream(),
        ScriptedWaiter([WorkEvent(kind="text", text="Hello"), WorkEvent(kind="done")]),
        _noop_cancel,
        on_stream_complete=on_stream_complete,
    )

    assert order == ["attachments", "end"]


@pytest.mark.asyncio
async def test_error_before_any_text_is_streamed_as_a_generic_message_and_commits_streaming():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([WorkEvent(kind="error", text="boom")])

    mode = await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert stream.text_chunks == [PRE_TEXT_ERROR_MESSAGE]
    assert stream.ended is True
    assert mode is DeliveryMode.STREAMING
    assert broker.mode is DeliveryMode.STREAMING


@pytest.mark.asyncio
async def test_error_after_text_appends_a_short_failure_message_and_keeps_streaming():
    broker = WorkEventBroker()
    stream = FakeStream()
    waiter = ScriptedWaiter([WorkEvent(kind="text", text="Working on it: "), WorkEvent(kind="error", text="boom")])

    mode = await consume_activity_turn(broker, stream, waiter, _noop_cancel)

    assert stream.text_chunks == ["Working on it: ", POST_TEXT_ERROR_SUFFIX]
    assert stream.ended is True
    assert mode is DeliveryMode.STREAMING


@pytest.mark.asyncio
async def test_cancellation_detected_after_a_send_stops_the_turn_and_calls_on_cancel():
    broker = WorkEventBroker()
    stream = FakeStream(cancel_after="text")
    waiter = ScriptedWaiter([WorkEvent(kind="text", text="Hello")])
    on_cancel_calls: list[bool] = []

    async def on_cancel() -> None:
        on_cancel_calls.append(True)

    await consume_activity_turn(broker, stream, waiter, on_cancel)

    assert on_cancel_calls == [True]
    assert stream.ended is True


@pytest.mark.asyncio
async def test_cancellation_before_any_text_leaves_the_delivery_mode_undecided():
    broker = WorkEventBroker()
    stream = FakeStream(cancel_after="progress")
    waiter = ScriptedWaiter([WorkEvent(kind="progress", text="Looking...")])
    on_cancel_calls: list[bool] = []

    async def on_cancel() -> None:
        on_cancel_calls.append(True)

    mode = await consume_activity_turn(broker, stream, waiter, on_cancel)

    assert on_cancel_calls == [True]
    assert stream.ended is True
    assert mode is None
    assert broker.mode is None


@pytest.mark.asyncio
async def test_activity_stream_private_cancel_flag_is_detected_when_no_public_property_exists():
    class PrivateCancelStream:
        def __init__(self) -> None:
            self._cancelled = False
            self.ended = False

        def queue_informative_update(self, _text: str) -> None:
            self._cancelled = True

        def queue_text_chunk(self, _text: str) -> None:
            pass

        async def end_stream(self) -> None:
            self.ended = True

    broker = WorkEventBroker()
    stream = PrivateCancelStream()
    waiter = ScriptedWaiter([WorkEvent(kind="progress", text="Looking...")])
    cancelled: list[bool] = []

    async def on_cancel() -> None:
        cancelled.append(True)

    await consume_activity_turn(broker, stream, waiter, on_cancel)

    assert cancelled == [True]
    assert stream.ended is True


# ── WorkInput ────────────────────────────────────────────────────────────────


def test_work_input_accepts_the_required_fields_with_optional_defaults():
    work_input = WorkInput(conversation_id="conv-1", prompt="hello")

    assert work_input.conversation_id == "conv-1"
    assert work_input.prompt == "hello"
    assert work_input.files is None
    assert work_input.call_id is None


def test_work_input_accepts_optional_files_and_a_top_level_call_id():
    work_input = WorkInput(
        conversation_id="conv-1",
        prompt="hello",
        files=[{"name": "a.txt", "path": "/tmp/a.txt"}],
        call_id="call-123",
    )

    assert work_input.files == [{"name": "a.txt", "path": "/tmp/a.txt"}]
    assert work_input.call_id == "call-123"


def test_work_input_rejects_unknown_top_level_fields():
    with pytest.raises(ValidationError):
        WorkInput(conversation_id="conv-1", prompt="hello", unexpected="nope")


def test_work_input_requires_prompt():
    with pytest.raises(ValidationError):
        WorkInput(conversation_id="conv-1")


# ── execute_work ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_work_sends_a_proactive_notification_when_no_broker_is_attached():
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), asyncio.Event())
    registry = BrokerRegistry()  # nothing registered for "task-1" -> recovery path

    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("delta", "Hello ")
        yield ("delta", "world.")

    sent: list[tuple[str, str]] = []

    async def send_proactive(conversation_id, text):
        sent.append((conversation_id, text))

    async def abort(conversation_id):
        raise AssertionError("abort should not be called")

    await execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)

    assert sent == [("conv-1", "Hello world.")]
    assert registry.get("task-1") is None


@pytest.mark.asyncio
async def test_execute_work_sends_a_proactive_notification_when_the_attached_broker_is_deferred():
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), asyncio.Event())
    registry = BrokerRegistry()
    broker = registry.create("task-1")
    broker.commit_delivery_mode(DeliveryMode.DEFERRED)

    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("final", "The complete answer.")

    sent: list[tuple[str, str]] = []

    async def send_proactive(conversation_id, text):
        sent.append((conversation_id, text))

    async def abort(conversation_id):
        raise AssertionError("abort should not be called")

    await execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)

    assert sent == [("conv-1", "The complete answer.")]
    assert registry.get("task-1") is None


@pytest.mark.asyncio
async def test_execute_work_publishes_progress_text_then_done_and_skips_proactive_when_streaming():
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), asyncio.Event())
    registry = BrokerRegistry()
    broker = registry.create("task-1")
    broker.commit_delivery_mode(DeliveryMode.STREAMING)

    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("progress", "Thinking...")
        yield ("delta", "Hello")

    async def send_proactive(conversation_id, text):
        raise AssertionError("send_proactive should not be called")

    async def abort(conversation_id):
        raise AssertionError("abort should not be called")

    await execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)

    first = await broker.next_event()
    second = await broker.next_event()
    third = await broker.next_event()

    assert first == WorkEvent(kind="progress", text="Thinking...")
    assert second == WorkEvent(kind="text", text="Hello")
    assert third == WorkEvent(kind="done")
    assert registry.get("task-1") is None


@pytest.mark.asyncio
async def test_execute_work_waits_for_the_activity_handler_to_choose_a_delivery_mode():
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), asyncio.Event())
    registry = BrokerRegistry()
    broker = registry.create("task-1")

    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("delta", "Fast answer")

    async def send_proactive(conversation_id, text):
        raise AssertionError("streaming work must not notify proactively")

    async def abort(conversation_id):
        raise AssertionError("abort should not be called")

    worker = asyncio.create_task(
        execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)
    )
    first = await broker.next_event()
    broker.commit_delivery_mode(DeliveryMode.STREAMING)
    await worker
    done = await broker.next_event()

    assert first == WorkEvent(kind="text", text="Fast answer")
    assert done == WorkEvent(kind="done")


@pytest.mark.asyncio
async def test_execute_work_aborts_and_skips_notification_when_cancelled_before_any_output():
    cancel = asyncio.Event()
    cancel.set()
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), cancel)
    registry = BrokerRegistry()

    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("delta", "should not be reached")

    aborts: list[str] = []

    async def abort(conversation_id):
        aborts.append(conversation_id)

    async def send_proactive(conversation_id, text):
        raise AssertionError("send_proactive should not be called")

    await execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)

    assert aborts == ["conv-1"]


@pytest.mark.asyncio
async def test_execute_work_does_not_enter_the_model_stream_when_already_cancelled():
    cancel = asyncio.Event()
    cancel.set()
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), cancel)
    registry = BrokerRegistry()
    entered = False

    async def fake_ask_stream(conversation_id, prompt, files):
        nonlocal entered
        entered = True
        yield ("delta", "should not be reached")

    async def abort(conversation_id):
        pass

    async def send_proactive(conversation_id, text):
        raise AssertionError("send_proactive should not be called")

    await execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)

    assert entered is False


@pytest.mark.asyncio
async def test_execute_work_honors_cancellation_set_as_the_model_stream_ends():
    cancel = asyncio.Event()
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), cancel)
    registry = BrokerRegistry()
    broker = registry.create("task-1")
    broker.commit_delivery_mode(DeliveryMode.DEFERRED)

    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("delta", "Partial answer")
        cancel.set()

    aborts: list[str] = []
    sent: list[tuple[str, str]] = []

    async def abort(conversation_id):
        aborts.append(conversation_id)

    async def send_proactive(conversation_id, text):
        sent.append((conversation_id, text))

    await execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)

    assert aborts == ["conv-1"]
    assert sent == []
    assert await broker.next_event() == WorkEvent(kind="text", text="Partial answer")
    assert await broker.next_event() == WorkEvent(kind="error")


@pytest.mark.asyncio
async def test_execute_work_publishes_an_error_and_cleans_up_when_model_stream_raises():
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), asyncio.Event())
    registry = BrokerRegistry()
    broker = registry.create("task-1")
    broker.commit_delivery_mode(DeliveryMode.STREAMING)

    async def failing_ask_stream(conversation_id, prompt, files):
        yield ("delta", "Partial")
        raise RuntimeError("model failed")

    async def send_proactive(conversation_id, text):
        raise AssertionError("send_proactive should not be called")

    async def abort(conversation_id):
        raise AssertionError("abort should not be called")

    with pytest.raises(RuntimeError, match="model failed"):
        await execute_work(ctx, registry, failing_ask_stream, send_proactive, abort)

    assert await broker.next_event() == WorkEvent(kind="text", text="Partial")
    assert await broker.next_event() == WorkEvent(kind="error")
    assert registry.get("task-1") is None


@pytest.mark.asyncio
async def test_execute_work_stops_forwarding_once_cancel_is_set_mid_stream():
    cancel = asyncio.Event()
    ctx = FakeCtx("task-1", WorkInput(conversation_id="conv-1", prompt="hi"), cancel)
    registry = BrokerRegistry()
    broker = registry.create("task-1")
    broker.commit_delivery_mode(DeliveryMode.STREAMING)

    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("delta", "Part one.")
        cancel.set()
        yield ("delta", "Part two - should not be forwarded.")

    aborts: list[str] = []

    async def abort(conversation_id):
        aborts.append(conversation_id)

    async def send_proactive(conversation_id, text):
        raise AssertionError("send_proactive should not be called")

    await execute_work(ctx, registry, fake_ask_stream, send_proactive, abort)

    first = await broker.next_event()
    second = await broker.next_event()

    assert first == WorkEvent(kind="text", text="Part one.")
    assert second == WorkEvent(kind="error")
    assert aborts == ["conv-1"]
    assert registry.get("task-1") is None
