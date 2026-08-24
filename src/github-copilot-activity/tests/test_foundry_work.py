"""Tests for the not-yet-implemented `foundry_work` thin ``@task`` adapter.

Design ref (main.py refactor, part A): ``foundry_work.py`` is the single,
thin seam between the Foundry resilient-task SDK and the already-tested,
SDK-independent ``long_running.execute_work`` / ``long_running.WorkInput``.
No mock framework: every collaborator (the upstream ``ask_stream``/
``abort_turn`` shape, the configured proactive-sender callback, and the
injected ``task_object`` for ``start_work``) is a tiny hand-written async
fake. The real Foundry ``TaskManager`` is never constructed or started by
these tests -- the decorated ``Task`` handle's ``.start`` is only ever
exercised via an injected fake ``task_object``, and the handler itself is
called directly (bypassing the task manager entirely) via the plain,
undecorated function it wraps.

Expected (not yet implemented) production module: ``foundry_work``

  - At import time, calls
    ``azure.ai.agentserver.core.tasks.set_resilient_tasks_enabled(True)``
    before any ``AgentServerHost`` is constructed (see ``main.py``).
  - ``BROKERS = long_running.BrokerRegistry()`` -- module-level, shared
    broker registry for every task started through this adapter.
  - ``configure_proactive_sender(callback)`` -- stores the async
    ``(conversation_id, text) -> None`` callback used for the DEFERRED /
    no-broker delivery path; calling it a second time replaces the
    previously configured callback (no accumulation).
  - ``_chief_of_staff_turn(ctx)`` -- the plain async handler function
    (``ctx: TaskContext[long_running.WorkInput]``) wrapped by
    ``@task(name="chief-of-staff-turn")`` to produce the public
    ``chief_of_staff_turn`` ``Task`` object. Delegates to
    ``long_running.execute_work(ctx, BROKERS, client.ask_stream,
    <configured callback>, client.abort_turn)``. Raises a clear
    ``RuntimeError`` if no callback has been configured yet.
  - ``async start_work(task_id, work_input, *, task_object=chief_of_staff_turn)``
    -- calls ``await task_object.start(task_id=task_id, input=work_input)``
    and returns whatever that call returns (a ``TaskRun`` in production; a
    fake in tests). ``task_object`` is injectable specifically so tests
    never touch the real task manager.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.tasks import TaskContext, resilient_tasks_enabled

from long_running import BrokerRegistry, WorkInput  # type: ignore[import-not-found]

import foundry_work  # type: ignore[import-not-found]


def _ctx(task_id: str, work_input: WorkInput) -> TaskContext:
    return TaskContext(
        task_id=task_id,
        session_id="session-1",
        input=work_input,
        cancel=asyncio.Event(),
        shutdown=asyncio.Event(),
    )


def test_resilient_tasks_enabled_is_true_after_importing_foundry_work():
    assert resilient_tasks_enabled() is True


def test_brokers_is_a_broker_registry_instance():
    assert isinstance(foundry_work.BROKERS, BrokerRegistry)


@pytest.mark.asyncio
async def test_chief_of_staff_turn_raises_runtime_error_when_no_proactive_sender_is_configured():
    # Reload for a pristine module (no callback configured yet), independent
    # of whatever other tests in this file have already configured.
    importlib.reload(foundry_work)
    work_input = WorkInput(conversation_id="conv-1", prompt="hi")

    with pytest.raises(RuntimeError):
        await foundry_work._chief_of_staff_turn(_ctx("task-1", work_input))


@pytest.mark.asyncio
async def test_configure_proactive_sender_called_twice_replaces_the_first_callback(monkeypatch):
    async def fake_ask_stream(conversation_id, prompt, files):
        yield ("final", "the answer")

    async def fake_abort_turn(conversation_id):
        pass

    monkeypatch.setattr(foundry_work.client, "ask_stream", fake_ask_stream)
    monkeypatch.setattr(foundry_work.client, "abort_turn", fake_abort_turn)

    first_calls: list[tuple[str, str]] = []
    second_calls: list[tuple[str, str]] = []

    async def first_sender(conversation_id, text):
        first_calls.append((conversation_id, text))

    async def second_sender(conversation_id, text):
        second_calls.append((conversation_id, text))

    foundry_work.configure_proactive_sender(first_sender)
    foundry_work.configure_proactive_sender(second_sender)

    work_input = WorkInput(conversation_id="conv-1", prompt="hi")
    await foundry_work._chief_of_staff_turn(_ctx("task-2", work_input))

    assert first_calls == []
    assert second_calls == [("conv-1", "the answer")]


@pytest.mark.asyncio
async def test_chief_of_staff_turn_delegates_to_execute_work_with_client_ask_stream_and_abort_turn(monkeypatch):
    seen_ask_stream_args: list[tuple[str, str, object]] = []

    async def fake_ask_stream(conversation_id, prompt, files):
        seen_ask_stream_args.append((conversation_id, prompt, files))
        yield ("final", "hello world")

    abort_calls: list[str] = []

    async def fake_abort_turn(conversation_id):
        abort_calls.append(conversation_id)

    monkeypatch.setattr(foundry_work.client, "ask_stream", fake_ask_stream)
    monkeypatch.setattr(foundry_work.client, "abort_turn", fake_abort_turn)

    sent: list[tuple[str, str]] = []

    async def sender(conversation_id, text):
        sent.append((conversation_id, text))

    foundry_work.configure_proactive_sender(sender)
    work_input = WorkInput(conversation_id="conv-3", prompt="what's up")

    await foundry_work._chief_of_staff_turn(_ctx("task-3", work_input))

    assert seen_ask_stream_args == [("conv-3", "what's up", None)]
    assert sent == [("conv-3", "hello world")]
    assert abort_calls == []


@pytest.mark.asyncio
async def test_recovered_turn_rebinds_persisted_call_id_for_outbound_foundry_calls(monkeypatch):
    observed_call_ids: list[str | None] = []

    async def fake_ask_stream(conversation_id, prompt, files):
        observed_call_ids.append(get_request_context().call_id)
        yield ("final", "hello")

    async def fake_abort_turn(conversation_id):
        pass

    async def sender(conversation_id, text):
        pass

    monkeypatch.setattr(foundry_work.client, "ask_stream", fake_ask_stream)
    monkeypatch.setattr(foundry_work.client, "abort_turn", fake_abort_turn)
    foundry_work.configure_proactive_sender(sender)

    work_input = WorkInput(
        conversation_id="conv-5",
        prompt="recover",
        call_id="persisted-call-id",
    )

    await foundry_work._chief_of_staff_turn(_ctx("task-5", work_input))

    assert observed_call_ids == ["persisted-call-id"]
    assert get_request_context().call_id is None


@pytest.mark.asyncio
async def test_start_work_starts_the_injected_task_object_and_returns_its_task_run():
    calls: list[tuple[str, WorkInput]] = []

    class FakeTaskObject:
        async def start(self, *, task_id, input):  # noqa: A002 - mirrors the SDK's kwarg name
            calls.append((task_id, input))
            return "fake-task-run"

    work_input = WorkInput(conversation_id="conv-4", prompt="ping")

    result = await foundry_work.start_work("task-4", work_input, task_object=FakeTaskObject())

    assert calls == [("task-4", work_input)]
    assert result == "fake-task-run"
