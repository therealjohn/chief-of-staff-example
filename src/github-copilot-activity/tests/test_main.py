"""Tests for the not-yet-implemented `main.py` refactor (part B).

Design ref: ``main.py`` is refactored around small, injectable, testable
helpers so upstream card/file/invoke behavior keeps working while adding
installation-based proactive registration and Foundry-task-backed long-
running turns. No mock framework: every collaborator is a tiny hand-written
fake (mirroring the style already used by ``tests/test_agent_runtime.py``).
These tests never construct a real ``RecipientRepository`` (Azure storage),
never start the real Foundry task manager, and never run a real Starlette/
Hypercorn server.

Expected (not yet implemented) additions to the production module ``main``:

  - ``class MultiProtocolHost(ActivityAgentServerHost, InvocationAgentServerHost)``
    -- module-level ``host = MultiProtocolHost()``, ``app = host.agent_app``.
    Because ``AgentServerHost`` is a ``starlette.applications.Starlette``
    subclass, ``host.routes`` includes the ``/invocations`` POST route
    contributed by ``InvocationAgentServerHost.__init__``.

  - ``@dataclass class AppRuntime`` with fields ``repository``,
    ``recipients: RecipientService``, ``work: WorkService``.

  - ``async def _build_runtime(*, repository_factory=RecipientRepository.open_foundry,
    sender_factory=<factory returning ProactiveSender(host.adapter)>,
    work_start=foundry_work.start_work, abort_work=copilot_client.abort_turn) -> AppRuntime``
    -- awaits ``repository_factory()``, calls ``sender_factory()``, builds
    ``RecipientService`` + ``WorkService(TaskCoordinator(), foundry_work.BROKERS,
    work_start, abort_work)``, and calls
    ``foundry_work.configure_proactive_sender(callback)`` where ``callback``
    is an async ``(conversation_id, text) -> None`` that calls
    ``recipients.continue_conversation(conversation_id, send)`` with a
    ``send(turn_context)`` that does ``await turn_context.send_activity(text)``
    then ``await _deliver_ui(turn_context, conversation_id, None)``. All four
    factories/callables are injectable specifically so tests never touch
    Azure storage or the real Foundry task manager.

  - Module-level ``_runtime: AppRuntime | None`` cache plus
    ``async def _get_runtime() -> AppRuntime`` that calls ``_build_runtime()``
    exactly once and returns the same cached instance thereafter.

  - ``async def _shutdown_runtime(runtime: AppRuntime) -> None`` that awaits
    ``runtime.work.shutdown()``, ``copilot_client.close()``, and
    ``runtime.repository.close()``.

  - ``async def handle_installation_update(context, runtime) -> None`` --
    maps ``context.activity.action`` ``"add" -> "install"``,
    ``"remove" -> "uninstall"``, passes ``"add-upgrade"``/``"remove-upgrade"``
    through unchanged, and calls ``runtime.recipients.capture(context, mapped)``.
    Only when the ORIGINAL action was ``"add"`` and the capture result is
    ``"registered"`` does it send exactly one welcome message mentioning both
    "Chief of Staff" and what needs the user's attention without prescribing a
    command phrase. ``"remove"`` sends nothing. The upgrade actions send
    nothing (no separate SSO / welcome).

  - ``async def handle_message(context, runtime, *, download_shared_files=files.download_shared_files) -> None``
    -- rejects a non-personal conversation with a clear personal-chat-only
    reply (no recipient refresh, no work started). For a personal
    conversation: always calls ``runtime.recipients.capture(context, "message")``
    first. The reserved, case-insensitive, trimmed command ``"cancel"`` calls
    ``runtime.work.cancel(conversation_id)`` and replies whether a task was
    cancelled or none was active -- never calling ``runtime.work.start``.
    Otherwise builds a ``long_running.WorkInput`` (using
    ``azure.ai.agentserver.core.get_request_context().call_id`` for
    ``WorkInput.call_id``) and calls ``runtime.work.start``. A ``"busy"``
    result sends a short reply containing the active task id and never
    touches ``context.streaming_response``. A ``"started"`` result uses
    ``context.streaming_response`` and ``long_running.consume_activity_turn``
    with a waiter that does ``asyncio.wait_for(broker.next_event(), timeout)``
    when ``timeout`` is not ``None`` (returning ``None`` on
    ``asyncio.TimeoutError``) and awaits ``broker.next_event()`` directly when
    ``timeout`` is ``None``; its ``on_cancel`` calls ``runtime.work.cancel``;
    its ``on_stream_complete`` calls the existing ``_deliver_ui`` BEFORE the
    stream ends (so the deferred path, which never reaches
    ``on_stream_complete``, never double-delivers UI).

  - ``async def handle_notification_invocation(payload: dict, runtime) -> dict``
    -- validates ``payload`` against ``invocations.NotificationInvocationPayload``
    (raises ``pydantic.ValidationError`` on an invalid shape) and returns
    ``{"sent": ..., "failed": ..., "pruned": ...}`` from
    ``runtime.recipients.broadcast(notification)``. There is no
    daily-briefing-shaped branch.

  - ``async def on_notification_invocation(request)`` -- registered via
    ``@host.invoke_handler``; parses ``await request.json()``, calls
    ``handle_notification_invocation(payload, await _get_runtime())``, and
    returns a ``starlette.responses.JSONResponse`` (200 with the counts, or
    400 on any invalid-JSON/invalid-schema failure).
"""

from __future__ import annotations

import os

# `main` constructs a real ActivityAgentServerHost at import time, which
# configures OpenTelemetry tracing/metrics. Disable the SDK before that
# import so these tests never attempt any live Azure/network call (no
# Application Insights / IMDS statsbeat traffic) -- set only if the
# environment has not already opted into telemetry explicitly.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from azure.ai.agentserver.activity import ActivityAgentServerHost  # type: ignore[import-not-found]
from azure.ai.agentserver.invocations import InvocationAgentServerHost  # type: ignore[import-not-found]
from azure.ai.agentserver.core import (  # type: ignore[import-not-found]
    FoundryAgentRequestContext,
    reset_request_context,
    set_request_context,
)

from agent_runtime import RecipientService, WorkService, WorkStart  # type: ignore[import-not-found]
from long_running import WorkEvent, WorkEventBroker  # type: ignore[import-not-found]
from notification_delivery import FanOutResult  # type: ignore[import-not-found]

import main  # type: ignore[import-not-found]


# ── shared tiny fakes ────────────────────────────────────────────────────────


async def _no_shared_files(activity, conversation_id, on_progress=None, connector=None):
    return []


def _activity(*, conversation_type: str, conversation_id: str = "conv-1", text: str = "", action=None):
    return SimpleNamespace(
        conversation=SimpleNamespace(id=conversation_id, conversation_type=conversation_type),
        text=text,
        action=action,
        attachments=[],
    )


class _FakeStream:
    def __init__(self) -> None:
        self.informative: list[str] = []
        self.text_chunks: list[str] = []
        self.attachments: list[object] = []
        self.ended = False
        self.cancelled = False

    def queue_informative_update(self, text: str) -> None:
        self.informative.append(text)

    def queue_text_chunk(self, text: str) -> None:
        self.text_chunks.append(text)

    def add_attachment(self, attachment: object) -> None:
        self.attachments.append(attachment)

    async def end_stream(self) -> None:
        self.ended = True


class _FakeContext:
    """Tiny fake standing in for the M365 TurnContext used by the helpers."""

    def __init__(self, activity) -> None:
        self.activity = activity
        self.sent: list[object] = []
        self.turn_state: dict = {}
        self.streaming_access_count = 0
        self._stream: _FakeStream | None = None

    async def send_activity(self, activity_or_text) -> None:
        self.sent.append(activity_or_text)

    @property
    def streaming_response(self):
        self.streaming_access_count += 1
        if self._stream is None:
            raise RuntimeError("streaming not supported on this fake context")
        return self._stream


class _FakeRecipients:
    def __init__(self, capture_status: str = "registered") -> None:
        self.capture_calls: list[str] = []
        self._capture_status = capture_status

    async def capture(self, context, action: str) -> str:
        self.capture_calls.append(action)
        return self._capture_status


class _FakeWork:
    def __init__(self, *, start_result=None, cancel_result: str = "none") -> None:
        self.start_calls: list[tuple[str, object]] = []
        self.cancel_calls: list[str] = []
        self._start_result = start_result
        self._cancel_result = cancel_result

    async def start(self, conversation_id: str, work_input):
        self.start_calls.append((conversation_id, work_input))
        return self._start_result

    async def cancel(self, conversation_id: str) -> str:
        self.cancel_calls.append(conversation_id)
        return self._cancel_result


class _FakeRuntime:
    def __init__(self, recipients, work) -> None:
        self.recipients = recipients
        self.work = work


class _FakeRepository:
    def __init__(self, *recipients) -> None:
        self._recipients = list(recipients)
        self.closed = False

    async def all(self):
        return list(self._recipients)

    async def close(self) -> None:
        self.closed = True


class _FakeSender:
    def __init__(self) -> None:
        self.send_calls: list[tuple[str, str]] = []

    async def send(self, recipient, text: str) -> None:
        self.send_calls.append((recipient.conversation_id, text))


# ── host / route existence ──────────────────────────────────────────────────


def test_multi_protocol_host_combines_activity_and_invocation_hosts_and_exposes_app():
    assert issubclass(main.MultiProtocolHost, ActivityAgentServerHost)
    assert issubclass(main.MultiProtocolHost, InvocationAgentServerHost)
    assert isinstance(main.host, main.MultiProtocolHost)
    assert main.app is main.host.agent_app


def test_invocations_post_route_is_registered_on_the_multi_protocol_host():
    paths = {getattr(route, "path", None) for route in main.host.routes}
    assert "/invocations" in paths


# ── _build_runtime / _get_runtime / _shutdown_runtime ───────────────────────


@pytest.mark.asyncio
async def test_build_runtime_wires_repository_recipients_and_work_from_injected_factories(monkeypatch):
    repo = _FakeRepository()

    async def repository_factory():
        return repo

    sender = _FakeSender()

    async def fake_work_start(task_id, work_input):
        raise AssertionError("start_work should not be invoked while building the runtime")

    async def fake_abort_work(conversation_id):
        raise AssertionError("abort_work should not be invoked while building the runtime")

    configured: list[object] = []
    monkeypatch.setattr(main.foundry_work, "configure_proactive_sender", configured.append)

    runtime = await main._build_runtime(
        repository_factory=repository_factory,
        sender_factory=lambda: sender,
        work_start=fake_work_start,
        abort_work=fake_abort_work,
    )

    assert isinstance(runtime, main.AppRuntime)
    assert runtime.repository is repo
    assert isinstance(runtime.recipients, RecipientService)
    assert isinstance(runtime.work, WorkService)
    assert len(configured) == 1

    await runtime.work.shutdown()


@pytest.mark.asyncio
async def test_build_runtime_configures_a_proactive_sender_that_delivers_text_then_ui(monkeypatch):
    recipient = SimpleNamespace(conversation_id="conv-1")
    repo = _FakeRepository(recipient)

    async def repository_factory():
        return repo

    proactive_turn_context = SimpleNamespace(sent=[])

    async def _send_activity(text):
        proactive_turn_context.sent.append(text)

    proactive_turn_context.send_activity = _send_activity

    class _ContinueSender(_FakeSender):
        def __init__(self) -> None:
            super().__init__()
            self.continued_ids: list[str] = []

        async def continue_conversation(self, recipient, callback):
            self.continued_ids.append(recipient.conversation_id)
            await callback(proactive_turn_context)

    sender = _ContinueSender()

    configured: list[object] = []
    monkeypatch.setattr(main.foundry_work, "configure_proactive_sender", configured.append)

    deliver_calls: list[tuple[object, str, object]] = []

    async def fake_deliver_ui(context, conversation_id, stream):
        deliver_calls.append((context, conversation_id, stream))

    monkeypatch.setattr(main, "_deliver_ui", fake_deliver_ui)

    async def fake_work_start(task_id, work_input):
        return None

    async def fake_abort_work(conversation_id):
        return None

    await main._build_runtime(
        repository_factory=repository_factory,
        sender_factory=lambda: sender,
        work_start=fake_work_start,
        abort_work=fake_abort_work,
    )

    proactive_callback = configured[0]
    await proactive_callback("conv-1", "final answer text")

    assert sender.continued_ids == ["conv-1"]
    assert proactive_turn_context.sent == ["final answer text"]
    assert deliver_calls == [(proactive_turn_context, "conv-1", None)]


@pytest.mark.asyncio
async def test_get_runtime_builds_the_runtime_once_and_caches_it(monkeypatch):
    monkeypatch.setattr(main, "_runtime", None, raising=False)
    sentinel = object()
    build_calls = []

    async def fake_build_runtime(**kwargs):
        build_calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(main, "_build_runtime", fake_build_runtime)

    first = await main._get_runtime()
    second = await main._get_runtime()

    assert first is sentinel
    assert second is sentinel
    assert len(build_calls) == 1


@pytest.mark.asyncio
async def test_recovery_proactive_sender_is_configured_before_runtime_is_built(monkeypatch):
    runtime = object()
    delivered: list[tuple[object, str, str]] = []

    async def fake_get_runtime():
        return runtime

    async def fake_deliver(runtime_arg, conversation_id, text):
        delivered.append((runtime_arg, conversation_id, text))

    monkeypatch.setattr(main, "_get_runtime", fake_get_runtime)
    monkeypatch.setattr(main, "_deliver_proactive_result", fake_deliver)

    main._configure_recovery_sender()
    assert main.foundry_work._proactive_sender is main._recovery_proactive_sender

    await main.foundry_work._proactive_sender("conv-1", "Recovered answer")

    assert delivered == [(runtime, "conv-1", "Recovered answer")]


@pytest.mark.asyncio
async def test_get_runtime_is_single_flight_for_concurrent_first_requests(monkeypatch):
    monkeypatch.setattr(main, "_runtime", None, raising=False)
    build_entered = asyncio.Event()
    release_build = asyncio.Event()
    build_calls = 0
    sentinel = object()

    async def fake_build_runtime(**kwargs):
        nonlocal build_calls
        build_calls += 1
        build_entered.set()
        await release_build.wait()
        return sentinel

    monkeypatch.setattr(main, "_build_runtime", fake_build_runtime)

    first = asyncio.create_task(main._get_runtime())
    await build_entered.wait()
    second = asyncio.create_task(main._get_runtime())
    await asyncio.sleep(0)

    assert build_calls == 1

    release_build.set()
    assert await asyncio.gather(first, second) == [sentinel, sentinel]


@pytest.mark.asyncio
async def test_shutdown_runtime_closes_the_work_service_the_client_and_the_repository(monkeypatch):
    class _FakeWorkService:
        def __init__(self) -> None:
            self.shutdown_called = False

        async def shutdown(self) -> None:
            self.shutdown_called = True

    client_close_calls = []

    async def fake_close():
        client_close_calls.append(True)

    monkeypatch.setattr(main.copilot_client, "close", fake_close)

    work = _FakeWorkService()
    repo = _FakeRepository()
    runtime = main.AppRuntime(repository=repo, recipients=object(), work=work)

    await main._shutdown_runtime(runtime)

    assert work.shutdown_called is True
    assert client_close_calls == [True]
    assert repo.closed is True


# ── handle_installation_update ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_installation_update_add_action_registers_and_sends_one_chief_of_staff_welcome():
    context = _FakeContext(_activity(conversation_type="personal", action="add"))
    recipients = _FakeRecipients(capture_status="registered")
    runtime = _FakeRuntime(recipients, _FakeWork())

    await main.handle_installation_update(context, runtime)

    assert recipients.capture_calls == ["install"]
    assert len(context.sent) == 1
    welcome = context.sent[0]
    assert "Chief of Staff" in welcome
    assert "say '" not in welcome.lower()
    assert "what needs your attention" in welcome.lower()


@pytest.mark.asyncio
async def test_handle_installation_update_remove_action_unregisters_and_sends_nothing():
    context = _FakeContext(_activity(conversation_type="personal", action="remove"))
    recipients = _FakeRecipients(capture_status="removed")
    runtime = _FakeRuntime(recipients, _FakeWork())

    await main.handle_installation_update(context, runtime)

    assert recipients.capture_calls == ["uninstall"]
    assert context.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add-upgrade", "remove-upgrade"])
async def test_handle_installation_update_upgrade_actions_pass_through_unchanged_and_send_nothing(action):
    context = _FakeContext(_activity(conversation_type="personal", action=action))
    recipients = _FakeRecipients(capture_status="preserved")
    runtime = _FakeRuntime(recipients, _FakeWork())

    await main.handle_installation_update(context, runtime)

    assert recipients.capture_calls == [action]
    assert context.sent == []


# ── handle_message ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_message_rejects_a_non_personal_conversation_with_a_personal_chat_only_reply():
    context = _FakeContext(_activity(conversation_type="channel", text="hello"))
    recipients = _FakeRecipients()
    work = _FakeWork()
    runtime = _FakeRuntime(recipients, work)

    await main.handle_message(context, runtime, download_shared_files=_no_shared_files)

    assert len(context.sent) == 1
    assert "personal" in context.sent[0].lower()
    assert recipients.capture_calls == []
    assert work.start_calls == []


@pytest.mark.asyncio
async def test_handle_message_refreshes_the_recipient_on_every_personal_message():
    context = _FakeContext(_activity(conversation_type="personal", text="cancel"))
    recipients = _FakeRecipients()
    work = _FakeWork(cancel_result="none")
    runtime = _FakeRuntime(recipients, work)

    await main.handle_message(context, runtime, download_shared_files=_no_shared_files)

    assert recipients.capture_calls == ["message"]


@pytest.mark.asyncio
async def test_handle_message_cancel_command_is_case_insensitive_and_trimmed_and_cancels_the_active_task():
    context = _FakeContext(_activity(conversation_type="personal", text="  Cancel  "))
    work = _FakeWork(cancel_result="task-99")
    runtime = _FakeRuntime(_FakeRecipients(), work)

    await main.handle_message(context, runtime, download_shared_files=_no_shared_files)

    assert work.cancel_calls == ["conv-1"]
    assert work.start_calls == []
    assert any("task-99" in str(msg) for msg in context.sent)


@pytest.mark.asyncio
async def test_handle_message_cancel_command_with_no_active_task_replies_without_starting_work():
    context = _FakeContext(_activity(conversation_type="personal", text="cancel"))
    work = _FakeWork(cancel_result="none")
    runtime = _FakeRuntime(_FakeRecipients(), work)

    await main.handle_message(context, runtime, download_shared_files=_no_shared_files)

    assert work.cancel_calls == ["conv-1"]
    assert work.start_calls == []
    assert len(context.sent) == 1
    assert "no active" in context.sent[0].lower()


@pytest.mark.asyncio
async def test_handle_message_busy_replies_with_the_active_task_id_and_never_touches_the_stream():
    busy = WorkStart(status="busy", task_id="busy-123", broker=None)
    context = _FakeContext(_activity(conversation_type="personal", text="do the thing"))
    work = _FakeWork(start_result=busy)
    runtime = _FakeRuntime(_FakeRecipients(), work)

    await main.handle_message(context, runtime, download_shared_files=_no_shared_files)

    assert any("busy-123" in str(msg) for msg in context.sent)
    assert context.streaming_access_count == 0


@pytest.mark.asyncio
async def test_handle_message_work_input_carries_the_request_contexts_call_id():
    token = set_request_context(FoundryAgentRequestContext(call_id="call-123"))
    try:
        busy = WorkStart(status="busy", task_id="t-1", broker=None)
        context = _FakeContext(_activity(conversation_type="personal", text="hello"))
        work = _FakeWork(start_result=busy)
        runtime = _FakeRuntime(_FakeRecipients(), work)

        await main.handle_message(context, runtime, download_shared_files=_no_shared_files)
    finally:
        reset_request_context(token)

    assert len(work.start_calls) == 1
    _conversation_id, work_input = work.start_calls[0]
    assert work_input.call_id == "call-123"


@pytest.mark.asyncio
async def test_handle_message_started_delivers_ui_before_ending_the_stream_and_never_duplicates_it(monkeypatch):
    broker = WorkEventBroker()
    await broker.publish(WorkEvent(kind="text", text="hello there"))
    await broker.publish(WorkEvent(kind="done"))
    started = WorkStart(status="started", task_id="t-1", broker=broker)

    context = _FakeContext(
        _activity(conversation_type="personal", text="what needs my attention?")
    )
    stream = _FakeStream()
    context._stream = stream
    work = _FakeWork(start_result=started)
    runtime = _FakeRuntime(_FakeRecipients(), work)

    deliver_calls: list[tuple[str, bool]] = []

    async def fake_deliver_ui(_context, conversation_id, stream_arg):
        deliver_calls.append((conversation_id, stream_arg.ended))

    monkeypatch.setattr(main, "_deliver_ui", fake_deliver_ui)

    await main.handle_message(context, runtime, download_shared_files=_no_shared_files)

    assert stream.text_chunks == ["hello there"]
    assert deliver_calls == [("conv-1", False)]
    assert stream.ended is True


@pytest.mark.asyncio
async def test_handle_message_sends_silent_timeout_ack_as_one_regular_reply(monkeypatch):
    broker = WorkEventBroker()
    started = WorkStart(status="started", task_id="t-1", broker=broker)
    context = _FakeContext(
        _activity(conversation_type="personal", text="what needs my attention?")
    )
    stream = _FakeStream()
    context._stream = stream
    runtime = _FakeRuntime(_FakeRecipients(), _FakeWork(start_result=started))

    async def immediate_timeout(coro, _timeout):
        coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)

    await main.handle_message(
        context,
        runtime,
        download_shared_files=_no_shared_files,
    )

    assert context.sent == [main.long_running.DEFERRED_MESSAGE]
    assert stream.text_chunks == []
    assert stream.ended is False


# ── generic Invocations (notification broadcast) ────────────────────────────


@pytest.mark.asyncio
async def test_handle_notification_invocation_returns_broadcast_counts_for_a_valid_payload():
    class _BroadcastRecipients:
        async def broadcast(self, text):
            assert text == "hello team"
            return FanOutResult(sent=2, failed=1, pruned=0)

    runtime = _FakeRuntime(_BroadcastRecipients(), _FakeWork())

    result = await main.handle_notification_invocation({"notification": "hello team"}, runtime)

    assert result == {"sent": 2, "failed": 1, "pruned": 0}


@pytest.mark.asyncio
async def test_handle_notification_invocation_accepts_foundry_routine_input_wrapper():
    class _BroadcastRecipients:
        async def broadcast(self, text):
            assert text == "routine notification"
            return FanOutResult(sent=1, failed=0, pruned=0)

    runtime = _FakeRuntime(_BroadcastRecipients(), _FakeWork())

    result = await main.handle_notification_invocation(
        {"input": {"notification": "routine notification"}},
        runtime,
    )

    assert result == {"sent": 1, "failed": 0, "pruned": 0}


@pytest.mark.asyncio
async def test_handle_notification_invocation_raises_validation_error_for_an_invalid_payload():
    runtime = _FakeRuntime(_FakeRecipients(), _FakeWork())

    with pytest.raises(ValidationError):
        await main.handle_notification_invocation({"briefingType": "daily"}, runtime)


@pytest.mark.asyncio
async def test_on_notification_invocation_returns_a_400_json_response_for_an_invalid_payload(monkeypatch):
    runtime = _FakeRuntime(_FakeRecipients(), _FakeWork())

    async def fake_get_runtime():
        return runtime

    monkeypatch.setattr(main, "_get_runtime", fake_get_runtime)

    class _FakeRequest:
        async def json(self):
            return {"notification": ""}

    response = await main.on_notification_invocation(_FakeRequest())

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_on_notification_invocation_propagates_internal_value_errors(monkeypatch):
    class FailingRecipients:
        async def broadcast(self, _text):
            raise ValueError("corrupt recipient state")

    runtime = _FakeRuntime(FailingRecipients(), _FakeWork())

    async def fake_get_runtime():
        return runtime

    monkeypatch.setattr(main, "_get_runtime", fake_get_runtime)

    class _FakeRequest:
        async def json(self):
            return {"notification": "hello"}

    with pytest.raises(ValueError, match="corrupt recipient state"):
        await main.on_notification_invocation(_FakeRequest())
