"""Thin Foundry ``@task`` adapter for the chief-of-staff long-running turn.

This module is the single, thin seam between the Foundry resilient-task SDK
(``azure.ai.agentserver.core.tasks``) and the already-tested, SDK-independent
``long_running.execute_work`` / ``long_running.WorkInput``. It has no other
responsibilities: no business logic lives here.

Importing this module has the side effect of opting the process into the
Foundry resilient task subsystem (``set_resilient_tasks_enabled(True)``).
This must happen before ``main.py`` constructs its ``AgentServerHost``, so
``main.py`` imports this module before building the host.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from azure.ai.agentserver.core import (
    FoundryAgentRequestContext,
    get_request_context,
    reset_request_context,
    set_request_context,
)
from azure.ai.agentserver.core.tasks import TaskContext, set_resilient_tasks_enabled, task

import client
import long_running
from long_running import BrokerRegistry, WorkInput

# Opt in to the resilient task subsystem at import time (before any
# AgentServerHost is constructed -- see main.py import ordering).
set_resilient_tasks_enabled(True)

# Shared broker registry for every task started through this adapter.
BROKERS = BrokerRegistry()

# The async (conversation_id, text) -> None callback used for the DEFERRED /
# no-broker delivery path. Configured by main.py's `_build_runtime`.
_proactive_sender: Callable[[str, str], Awaitable[None]] | None = None


def configure_proactive_sender(callback: Callable[[str, str], Awaitable[None]]) -> None:
    """Configure the proactive-delivery callback used by deferred/recovered turns.

    Calling this a second time replaces the previously configured callback
    (no accumulation).
    """
    global _proactive_sender  # pylint: disable=global-statement
    _proactive_sender = callback


async def _chief_of_staff_turn(ctx: TaskContext[WorkInput]) -> None:
    """Plain, undecorated handler wrapped by ``@task`` below.

    Delegates to ``long_running.execute_work`` using the upstream Copilot
    client's ``ask_stream``/``abort_turn`` shape and the configured proactive
    callback.
    """
    if _proactive_sender is None:
        raise RuntimeError(
            "no proactive sender configured; call foundry_work.configure_proactive_sender() first"
        )
    request_context_token = None
    if ctx.input.call_id and get_request_context().call_id != ctx.input.call_id:
        request_context_token = set_request_context(
            FoundryAgentRequestContext(call_id=ctx.input.call_id)
        )
    try:
        await long_running.execute_work(
            ctx,
            BROKERS,
            client.ask_stream,
            _proactive_sender,
            client.abort_turn,
        )
    finally:
        if request_context_token is not None:
            reset_request_context(request_context_token)


chief_of_staff_turn = task(name="chief-of-staff-turn")(_chief_of_staff_turn)


async def start_work(
    task_id: str,
    work_input: WorkInput,
    *,
    task_object: Any = chief_of_staff_turn,
) -> Any:
    """Start the chief-of-staff task for one conversation turn.

    ``task_object`` is injectable specifically so tests never touch the real
    Foundry task manager.
    """
    return await task_object.start(task_id=task_id, input=work_input)
