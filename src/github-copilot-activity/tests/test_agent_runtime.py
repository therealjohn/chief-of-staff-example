"""Tests for the not-yet-implemented `agent_runtime` module (RecipientService
and WorkService) -- the thin orchestration layer main.py is expected to use.

No mock framework: repository/sender/start_work/abort_work are tiny
hand-written async fakes, and the fake TaskRun is backed by a real
asyncio.Future (resolved explicitly by each test, never by sleeping).

Expected (not yet implemented) production module: ``agent_runtime``

  - ``RecipientService(repository, sender)`` where ``repository`` exposes
    async ``upsert``/``remove``/``all`` (see ``notification_delivery`` /
    ``recipient_repository``) and ``sender`` exposes async
    ``send(recipient, text)`` (see ``proactive_delivery.ProactiveSender``).
      - ``await capture(context, action) -> str`` uses
        ``proactive_delivery.recipient_from_turn_context``. For a personal
        context: "install"/"message" upsert and return "registered";
        "uninstall" removes and returns "removed"; "add-upgrade"/
        "remove-upgrade" preserve the existing record and return
        "preserved". A non-personal context is ignored (no repository call)
        and returns "ignored".
      - ``await send_to_conversation(conversation_id, text)`` finds the
        exact registered recipient (via ``repository.all()``) and calls
        ``sender.send(recipient, text)``; raises
        ``RecipientNotRegisteredError`` when no recipient is registered for
        that conversation id.
      - ``await broadcast(text)`` delegates to
        ``notification_delivery.async_notify_all`` and returns its
        ``FanOutResult`` (sent/failed/pruned counts).

  - ``WorkService(task_coordinator, broker_registry, start_work, abort_work)``
    where ``start_work`` is ``async (task_id, WorkInput) -> TaskRun``-like
    and ``abort_work`` is ``async (conversation_id) -> None``.
      - ``await start(conversation_id, WorkInput) -> WorkStart`` with
        ``.status`` in {"started", "busy"}, ``.task_id``, and ``.broker``
        (a ``long_running.WorkEventBroker``, only set when started -- None
        when busy). The first start calls ``task_coordinator.start``,
        creates a broker via ``broker_registry.create(task_id)``, calls
        ``start_work(task_id, work_input)`` with that same task id, retains
        the returned run, and starts a background watcher that awaits
        ``run.result()`` and then completes the coordinator and forgets the
        run. A second start while active returns "busy" with the existing
        task id and must not call ``start_work`` again nor create a second
        broker. If ``start_work`` raises, the coordinator slot and the
        broker are rolled back so the conversation can retry.
      - ``await cancel(conversation_id)`` calls ``task_coordinator.cancel``.
        When a task was active: awaits the saved run's ``.cancel()`` and
        ``abort_work(conversation_id)``, removes the broker/run, and
        returns the cancelled task id. With no active task: returns "none"
        and calls neither ``run.cancel()`` nor ``abort_work``.
      - ``await shutdown()`` cancels/awaits any watcher tasks owned by the
        service, so tests (and real process shutdown) leave no pending
        asyncio tasks behind.
"""

from __future__ import annotations

import asyncio

import pytest

from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount
from microsoft_agents.hosting.core.authorization import ClaimsIdentity

from notifications import NotificationRecipient  # type: ignore[import-not-found]
from long_running import BrokerRegistry, WorkInput  # type: ignore[import-not-found]
from task_coordinator import TaskCoordinator  # type: ignore[import-not-found]

from agent_runtime import (  # type: ignore[import-not-found]
    RecipientNotRegisteredError,
    RecipientService,
    WorkService,
)


# ── shared tiny fakes ────────────────────────────────────────────────────────


def _recipient(conversation_id: str) -> NotificationRecipient:
    return NotificationRecipient(
        conversation_id=conversation_id,
        is_personal=True,
        conversation_reference={"conversation": {"id": conversation_id}},
        claims={"tid": "tenant-1"},
    )


class _FakeRecipientRepository:
    """In-memory async double: no mock framework, just a dict + async methods."""

    def __init__(self, *recipients: NotificationRecipient) -> None:
        self._by_id = {r.conversation_id: r for r in recipients}

    async def upsert(self, recipient: NotificationRecipient) -> bool:
        created = recipient.conversation_id not in self._by_id
        self._by_id[recipient.conversation_id] = recipient
        return created

    async def remove(self, conversation_id: str) -> None:
        self._by_id.pop(conversation_id, None)

    async def all(self) -> list[NotificationRecipient]:
        return list(self._by_id.values())


class _FakeSender:
    """Complete async double for the ProactiveSender-shaped seam."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send(self, recipient: NotificationRecipient, text: str) -> None:
        self.calls.append((recipient.conversation_id, text))


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
    """Tiny fake at the external boundary, same shape used by proactive_delivery tests."""

    def __init__(self, activity: Activity, identity: ClaimsIdentity) -> None:
        self.activity = activity
        self.identity = identity


def _personal_context(conversation_id: str = "conv-1") -> _FakeTurnContext:
    return _FakeTurnContext(
        _activity("personal", conversation_id=conversation_id), ClaimsIdentity(claims={"tid": "tenant-1"})
    )


def _non_personal_context(conversation_id: str = "channel-conv-1") -> _FakeTurnContext:
    return _FakeTurnContext(
        _activity("channel", conversation_id=conversation_id), ClaimsIdentity(claims={"tid": "tenant-1"})
    )


class _FakeTaskRun:
    """Complete fake TaskRun backed by a real asyncio.Future -- no mock framework."""

    def __init__(self) -> None:
        self._future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.cancel_calls = 0

    async def result(self) -> None:
        return await self._future

    def finish(self) -> None:
        if not self._future.done():
            self._future.set_result(None)

    async def cancel(self) -> None:
        self.cancel_calls += 1
        if not self._future.done():
            self._future.set_result(None)


# ── RecipientService.capture ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_install_upserts_the_recipient_and_returns_registered():
    repo = _FakeRecipientRepository()
    service = RecipientService(repo, _FakeSender())

    status = await service.capture(_personal_context("conv-1"), "install")

    assert status == "registered"
    assert [r.conversation_id for r in await repo.all()] == ["conv-1"]


@pytest.mark.asyncio
async def test_capture_message_upserts_the_recipient_and_returns_registered():
    repo = _FakeRecipientRepository()
    service = RecipientService(repo, _FakeSender())

    status = await service.capture(_personal_context("conv-1"), "message")

    assert status == "registered"
    assert [r.conversation_id for r in await repo.all()] == ["conv-1"]


@pytest.mark.asyncio
async def test_duplicate_install_refreshes_recipient_without_reporting_a_new_registration():
    repo = _FakeRecipientRepository()
    service = RecipientService(repo, _FakeSender())
    context = _personal_context("conv-1")

    first = await service.capture(context, "install")
    second = await service.capture(context, "install")

    assert first == "registered"
    assert second == "refreshed"


@pytest.mark.asyncio
async def test_capture_uninstall_removes_the_recipient_and_returns_removed():
    repo = _FakeRecipientRepository()
    service = RecipientService(repo, _FakeSender())
    context = _personal_context("conv-1")
    await service.capture(context, "install")

    status = await service.capture(context, "uninstall")

    assert status == "removed"
    assert await repo.all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add-upgrade", "remove-upgrade"])
async def test_capture_upgrade_actions_preserve_the_existing_recipient_and_return_preserved(action):
    repo = _FakeRecipientRepository()
    service = RecipientService(repo, _FakeSender())
    context = _personal_context("conv-1")
    await service.capture(context, "install")
    before = await repo.all()

    status = await service.capture(context, action)

    assert status == "preserved"
    assert await repo.all() == before


@pytest.mark.asyncio
async def test_capture_non_personal_context_is_ignored_and_returns_ignored():
    repo = _FakeRecipientRepository()
    service = RecipientService(repo, _FakeSender())

    status = await service.capture(_non_personal_context(), "install")

    assert status == "ignored"
    assert await repo.all() == []


# ── RecipientService.send_to_conversation ───────────────────────────────────


@pytest.mark.asyncio
async def test_send_to_conversation_calls_sender_with_the_registered_recipient_and_text():
    repo = _FakeRecipientRepository(_recipient("conv-1"))
    sender = _FakeSender()
    service = RecipientService(repo, sender)

    await service.send_to_conversation("conv-1", "hello there")

    assert sender.calls == [("conv-1", "hello there")]


@pytest.mark.asyncio
async def test_send_to_conversation_raises_recipient_not_registered_error_when_missing():
    repo = _FakeRecipientRepository()
    service = RecipientService(repo, _FakeSender())

    with pytest.raises(RecipientNotRegisteredError) as exc_info:
        await service.send_to_conversation("conv-missing", "hello there")

    assert exc_info.value.conversation_id == "conv-missing"


@pytest.mark.asyncio
async def test_continue_conversation_uses_the_registered_recipient_and_callback():
    repo = _FakeRecipientRepository(_recipient("conv-1"))

    class ContinueSender(_FakeSender):
        def __init__(self) -> None:
            super().__init__()
            self.continued: list[str] = []

        async def continue_conversation(self, recipient, callback):
            self.continued.append(recipient.conversation_id)
            await callback("turn-context")

    sender = ContinueSender()
    service = RecipientService(repo, sender)
    seen: list[str] = []

    async def callback(turn_context):
        seen.append(turn_context)

    await service.continue_conversation("conv-1", callback)

    assert sender.continued == ["conv-1"]
    assert seen == ["turn-context"]


@pytest.mark.asyncio
async def test_continue_conversation_raises_when_recipient_is_missing():
    service = RecipientService(_FakeRecipientRepository(), _FakeSender())

    with pytest.raises(RecipientNotRegisteredError):
        await service.continue_conversation("conv-missing", lambda _context: None)


# ── RecipientService.broadcast ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_delegates_to_async_notify_all_and_returns_its_counts():
    repo = _FakeRecipientRepository(_recipient("conv-1"), _recipient("conv-2"))
    sender = _FakeSender()
    service = RecipientService(repo, sender)

    result = await service.broadcast("hello")

    assert sorted(sender.calls) == [("conv-1", "hello"), ("conv-2", "hello")]
    assert (result.sent, result.failed, result.pruned) == (2, 0, 0)


# ── WorkService.start ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_creates_a_broker_and_starts_work_with_the_coordinators_generated_task_id():
    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    start_calls: list[tuple[str, WorkInput]] = []

    async def start_work(task_id, work_input):
        start_calls.append((task_id, work_input))
        return _FakeTaskRun()

    async def abort_work(conversation_id):
        pass

    service = WorkService(coordinator, broker_registry, start_work, abort_work)
    work_input = WorkInput(conversation_id="conv-1", prompt="hello")

    result = await service.start("conv-1", work_input)

    assert result.status == "started"
    assert start_calls == [(result.task_id, work_input)]
    assert broker_registry.get(result.task_id) is result.broker

    await service.shutdown()


@pytest.mark.asyncio
async def test_second_start_while_active_returns_busy_without_starting_work_again_or_creating_a_second_broker():
    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    start_calls: list[str] = []

    async def start_work(task_id, work_input):
        start_calls.append(task_id)
        return _FakeTaskRun()

    async def abort_work(conversation_id):
        pass

    service = WorkService(coordinator, broker_registry, start_work, abort_work)
    work_input = WorkInput(conversation_id="conv-1", prompt="hello")
    first = await service.start("conv-1", work_input)

    second = await service.start("conv-1", work_input)

    assert second.status == "busy"
    assert second.task_id == first.task_id
    assert second.broker is None
    assert start_calls == [first.task_id]

    await service.shutdown()


@pytest.mark.asyncio
async def test_start_rolls_back_coordinator_and_broker_so_the_conversation_can_retry_after_start_work_raises():
    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    attempted_task_ids: list[str] = []

    async def start_work(task_id, work_input):
        attempted_task_ids.append(task_id)
        if len(attempted_task_ids) == 1:
            raise RuntimeError("boom")
        return _FakeTaskRun()

    async def abort_work(conversation_id):
        pass

    service = WorkService(coordinator, broker_registry, start_work, abort_work)
    work_input = WorkInput(conversation_id="conv-1", prompt="hello")

    with pytest.raises(RuntimeError):
        await service.start("conv-1", work_input)

    retry = await service.start("conv-1", work_input)

    assert retry.status == "started"
    assert len(attempted_task_ids) == 2
    assert broker_registry.get(retry.task_id) is retry.broker

    await service.shutdown()


@pytest.mark.asyncio
async def test_watcher_completes_the_coordinator_and_frees_the_conversation_once_the_run_resolves():
    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    run = _FakeTaskRun()

    async def start_work(task_id, work_input):
        return run

    async def abort_work(conversation_id):
        pass

    service = WorkService(coordinator, broker_registry, start_work, abort_work)
    work_input = WorkInput(conversation_id="conv-1", prompt="hello")
    started = await service.start("conv-1", work_input)

    run.finish()
    # Deterministic (zero-wall-time) yields so the background watcher, whose
    # only await is on `run.result()` (already resolved above), gets a turn.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    restarted = await service.start("conv-1", work_input)

    assert restarted.status == "started"
    assert restarted.task_id != started.task_id
    assert broker_registry.get(started.task_id) is None

    await service.shutdown()


# ── WorkService.cancel ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_with_no_active_task_returns_none_and_calls_neither_dependency():
    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    abort_calls: list[str] = []

    async def start_work(task_id, work_input):
        return _FakeTaskRun()

    async def abort_work(conversation_id):
        abort_calls.append(conversation_id)

    service = WorkService(coordinator, broker_registry, start_work, abort_work)

    result = await service.cancel("conv-never-started")

    assert result == "none"
    assert abort_calls == []

    await service.shutdown()


@pytest.mark.asyncio
async def test_cancel_awaits_the_saved_runs_cancel_and_abort_work_and_returns_the_cancelled_task_id():
    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    run = _FakeTaskRun()
    abort_calls: list[str] = []

    async def start_work(task_id, work_input):
        return run

    async def abort_work(conversation_id):
        abort_calls.append(conversation_id)

    service = WorkService(coordinator, broker_registry, start_work, abort_work)
    work_input = WorkInput(conversation_id="conv-1", prompt="hello")
    started = await service.start("conv-1", work_input)

    result = await service.cancel("conv-1")

    assert result == started.task_id
    assert run.cancel_calls == 1
    assert abort_calls == ["conv-1"]
    assert broker_registry.get(started.task_id) is None

    await service.shutdown()


@pytest.mark.asyncio
async def test_cancel_finishes_the_local_watcher_even_when_run_cancel_does_not_resolve_result():
    class SlowCancelRun(_FakeTaskRun):
        async def cancel(self) -> None:
            self.cancel_calls += 1

    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    run = SlowCancelRun()

    async def start_work(task_id, work_input):
        return run

    async def abort_work(conversation_id):
        pass

    service = WorkService(coordinator, broker_registry, start_work, abort_work)
    before = set(asyncio.all_tasks())
    await service.start("conv-1", WorkInput(conversation_id="conv-1", prompt="hello"))
    watcher = next(task for task in asyncio.all_tasks() - before if task is not asyncio.current_task())

    await service.cancel("conv-1")

    assert watcher.done()
    await service.shutdown()


# ── WorkService.shutdown ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_cancels_a_pending_watcher_task_so_no_task_leaks():
    coordinator = TaskCoordinator()
    broker_registry = BrokerRegistry()
    run = _FakeTaskRun()  # deliberately never resolved

    async def start_work(task_id, work_input):
        return run

    async def abort_work(conversation_id):
        pass

    service = WorkService(coordinator, broker_registry, start_work, abort_work)
    work_input = WorkInput(conversation_id="conv-1", prompt="hello")
    await service.start("conv-1", work_input)

    # Must not hang even though the run's future never resolves on its own.
    await asyncio.wait_for(service.shutdown(), timeout=1)
