"""One-active-task-per-conversation coordinator.

A small, in-memory coordinator -- independent of the Foundry task SDK -- that
enforces at most one active task per conversation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class TaskStartResult:
    status: str  # "started" | "busy"
    task_id: str


@dataclass
class TaskCancelResult:
    status: str  # "cancelled" | "none"
    task_id: str | None


class TaskCoordinator:
    """Enforces at most one active task per conversation."""

    def __init__(self) -> None:
        self._active: dict[str, str] = {}

    def start(self, conversation_id: str) -> TaskStartResult:
        active_task_id = self._active.get(conversation_id)
        if active_task_id is not None:
            return TaskStartResult(status="busy", task_id=active_task_id)

        task_id = str(uuid.uuid4())
        self._active[conversation_id] = task_id
        return TaskStartResult(status="started", task_id=task_id)

    def cancel(self, conversation_id: str) -> TaskCancelResult:
        task_id = self._active.pop(conversation_id, None)
        if task_id is None:
            return TaskCancelResult(status="none", task_id=None)
        return TaskCancelResult(status="cancelled", task_id=task_id)

    def complete(self, conversation_id: str, task_id: str) -> bool:
        if self._active.get(conversation_id) == task_id:
            del self._active[conversation_id]
            return True
        return False
