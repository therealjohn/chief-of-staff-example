"""Tests for the one-active-task-per-conversation coordinator.

Design ref (item 4): a small, in-memory coordinator -- independent of the
Foundry task SDK -- enforces at most one active task per conversation.

  * starting a task when none is active succeeds and returns a new task id
  * starting a second task while one is active returns a "busy" result that
    carries the ALREADY-active task's id (the second request is rejected)
  * ``cancel`` cancels/removes the active task and reports it
  * ``cancel`` with no active task reports that (no-op) state
  * completing the active task frees the conversation up for a new start

Expected (not yet implemented) production module: ``task_coordinator``
  - ``TaskCoordinator()``
    - ``.start(conversation_id) -> TaskStartResult`` with ``.status`` in
      {"started", "busy"} and ``.task_id`` (new id, or the active id if busy)
    - ``.cancel(conversation_id) -> TaskCancelResult`` with ``.status`` in
      {"cancelled", "none"} and ``.task_id`` (cancelled id, or None)
    - ``.complete(conversation_id, task_id) -> bool`` frees the slot only if
      ``task_id`` matches the currently active task
"""

from __future__ import annotations

from task_coordinator import TaskCoordinator  # type: ignore[import-not-found]


def test_starting_a_task_with_none_active_succeeds():
    coordinator = TaskCoordinator()

    result = coordinator.start("conv-1")

    assert result.status == "started"
    assert result.task_id


def test_second_start_while_one_is_active_reports_busy_with_the_active_task_id():
    coordinator = TaskCoordinator()
    first = coordinator.start("conv-1")

    second = coordinator.start("conv-1")

    assert second.status == "busy"
    assert second.task_id == first.task_id


def test_two_different_conversations_may_each_have_their_own_active_task():
    coordinator = TaskCoordinator()

    first = coordinator.start("conv-1")
    second = coordinator.start("conv-2")

    assert first.status == "started"
    assert second.status == "started"
    assert first.task_id != second.task_id


def test_cancel_removes_the_active_task_and_reports_cancelled():
    coordinator = TaskCoordinator()
    started = coordinator.start("conv-1")

    result = coordinator.cancel("conv-1")

    assert result.status == "cancelled"
    assert result.task_id == started.task_id


def test_cancel_with_no_active_task_reports_none_state():
    coordinator = TaskCoordinator()

    result = coordinator.cancel("conv-never-started")

    assert result.status == "none"
    assert result.task_id is None


def test_after_cancel_a_new_task_can_be_started_for_the_same_conversation():
    coordinator = TaskCoordinator()
    started = coordinator.start("conv-1")
    coordinator.cancel("conv-1")

    restarted = coordinator.start("conv-1")

    assert restarted.status == "started"
    assert restarted.task_id != started.task_id


def test_completing_the_active_task_frees_the_conversation_for_a_new_start():
    coordinator = TaskCoordinator()
    started = coordinator.start("conv-1")

    freed = coordinator.complete("conv-1", started.task_id)
    restarted = coordinator.start("conv-1")

    assert freed is True
    assert restarted.status == "started"


def test_completing_with_a_mismatched_task_id_is_ignored_and_task_stays_active():
    coordinator = TaskCoordinator()
    started = coordinator.start("conv-1")

    freed = coordinator.complete("conv-1", "some-other-stale-task-id")
    still_busy = coordinator.start("conv-1")

    assert freed is False
    assert still_busy.status == "busy"
    assert still_busy.task_id == started.task_id
