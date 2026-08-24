# Copyright (c) Microsoft. All rights reserved.

"""A simple personal assistant on the Activity protocol.

A minimal Teams agent that chats through the **GitHub Copilot SDK** (which runs
its own model + tool-calling loop) and exposes a few custom tools: a to-do list
and reading files the user shares in the chat.

Hosted by ``azure-ai-agentserver-activity`` for the Foundry platform contract
and bridged to the M365 Agents SDK for activity processing and outbound channel
delivery (e.g. Microsoft Teams). The ``/invocations`` Invocations protocol
(``azure-ai-agentserver-invocations``) is combined onto the same host to serve
generic proactive-notification broadcasts, and long-running chat turns are
backed by a crash-resilient Foundry ``@task`` (see ``foundry_work.py``).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from os import environ
from typing import Any, Callable

_LOG_LEVEL = environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s:%(lineno)d | %(message)s",
)
logger = logging.getLogger("github-copilot")

from pydantic import ValidationError
from starlette.responses import JSONResponse

from azure.ai.agentserver.activity import ActivityAgentServerHost
from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.invocations import InvocationAgentServerHost

import client as copilot_client
import files
import outfiles
import cards
import tools as _tools
import invocations
import long_running
from agent_runtime import RecipientService, WorkService
from proactive_delivery import ProactiveSender
from recipient_repository import RecipientRepository
from task_coordinator import TaskCoordinator

# Importing `foundry_work` enables the Foundry resilient-task subsystem
# (`set_resilient_tasks_enabled(True)`) as a side effect. This MUST happen
# before the `AgentServerHost` below is constructed.
import foundry_work


async def _deliver_ui(context, conversation_id, stream) -> None:
    """Drain model-requested UI (task board, generated files) and attach it.

    Tools can't send activities, so they queue requests (see tools.queue_ui);
    we render them here. On a streaming turn the attachments ride the final
    chunk; otherwise they go as separate messages.
    """
    from microsoft_agents.activity import Activity, ActivityTypes

    actions = _tools.drain_ui(conversation_id)
    for action in actions:
        try:
            if action.get("type") == "task_board":
                tasks = _tools.get_tasks(conversation_id)
                att, lead = cards.tasks_card_attachment(conversation_id, tasks), ""
            elif action.get("type") == "file":
                # The model created the file itself (via its shell/python tools)
                # and handed us the path; read the bytes and offer them for
                # download. See tools._deliver_file.
                import os
                path = action.get("path") or ""
                with open(path, "rb") as fh:
                    data = fh.read()
                att, lead = outfiles.build_file_consent(os.path.basename(path) or "document.txt", data)
            else:
                continue
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("UI render failed: %s", exc, exc_info=True)
            continue

        if stream is not None:
            if lead:
                stream.queue_text_chunk(lead)
            stream.add_attachment(att)
        else:
            if lead:
                await context.send_activity(lead)
            await context.send_activity(Activity(type=ActivityTypes.message, attachments=[att]))


class MultiProtocolHost(ActivityAgentServerHost, InvocationAgentServerHost):
    """Combines the Activity protocol (Teams/M365) with the Invocations protocol.

    ``AgentServerHost`` is a ``starlette.applications.Starlette`` subclass, so
    combining both protocol mixins here merges their routes onto a single app
    (e.g. ``/invocations`` alongside the Activity protocol's routes).
    """


# Simple Teams agent auth model is the default.
host = MultiProtocolHost()
app = host.agent_app


# ── Runtime wiring (repository / recipients / work) ─────────────────────────


@dataclass
class AppRuntime:
    """The small set of injectable collaborators every handler needs."""

    repository: Any
    recipients: RecipientService
    work: WorkService


async def _build_runtime(
    *,
    repository_factory: Callable[[], Any] = RecipientRepository.open_foundry,
    sender_factory: Callable[[], Any] = lambda: ProactiveSender(host.adapter),
    work_start: Callable[..., Any] = foundry_work.start_work,
    abort_work: Callable[[str], Any] = copilot_client.abort_turn,
) -> AppRuntime:
    """Build the process-wide runtime, wiring the recipient/work services.

    Also configures ``foundry_work``'s proactive-sender callback (used for
    the deferred / recovered-turn delivery path): it continues the recipient's
    conversation, sends the final text, then delivers any queued UI.
    """
    repository = await repository_factory()
    sender = sender_factory()
    recipients = RecipientService(repository, sender)
    work = WorkService(TaskCoordinator(), foundry_work.BROKERS, work_start, abort_work)

    async def _proactive_callback(conversation_id: str, text: str) -> None:
        await _deliver_proactive_result(
            AppRuntime(repository=repository, recipients=recipients, work=work),
            conversation_id,
            text,
        )

    foundry_work.configure_proactive_sender(_proactive_callback)

    return AppRuntime(repository=repository, recipients=recipients, work=work)


_runtime: "AppRuntime | None" = None
_runtime_lock = asyncio.Lock()


async def _get_runtime() -> AppRuntime:
    """Return the process-wide runtime, building it exactly once."""
    global _runtime  # pylint: disable=global-statement
    if _runtime is None:
        async with _runtime_lock:
            if _runtime is None:
                _runtime = await _build_runtime()
    return _runtime


async def _deliver_proactive_result(
    runtime: AppRuntime,
    conversation_id: str,
    text: str,
) -> None:
    async def _send(turn_context: Any) -> None:
        await turn_context.send_activity(text)
        await _deliver_ui(turn_context, conversation_id, None)

    await runtime.recipients.continue_conversation(conversation_id, _send)


async def _recovery_proactive_sender(conversation_id: str, text: str) -> None:
    runtime = await _get_runtime()
    await _deliver_proactive_result(runtime, conversation_id, text)


def _configure_recovery_sender() -> None:
    foundry_work.configure_proactive_sender(_recovery_proactive_sender)


_configure_recovery_sender()


async def _shutdown_runtime(runtime: AppRuntime) -> None:
    """Tear down the runtime's owned resources in dependency order."""
    await runtime.work.shutdown()
    await copilot_client.close()
    await runtime.repository.close()


# ── installationUpdate (register/unregister proactive recipients) ──────────

_INSTALLATION_ACTION_MAP = {"add": "install", "remove": "uninstall"}


async def handle_installation_update(context, runtime: AppRuntime) -> None:
    """Register/unregister the conversation as a proactive recipient.

    Only a fresh ``"add"`` (not an ``"add-upgrade"``) that results in a new
    registration gets a welcome message -- upgrades and removals send
    nothing.
    """
    action = context.activity.action
    mapped_action = _INSTALLATION_ACTION_MAP.get(action, action)
    status = await runtime.recipients.capture(context, mapped_action)

    if action == "add" and status == "registered":
        await context.send_activity(
            "Hi, I'm your Chief of Staff. Ask me anything, tell me to add a "
            "task, or ask what needs your attention."
        )


# ── message (chat turn) ──────────────────────────────────────────────────────


async def handle_message(
    context,
    runtime: AppRuntime,
    *,
    download_shared_files: Callable[..., Any] = files.download_shared_files,
) -> None:
    """Chat turn: fold in any shared files, then answer via the Copilot SDK."""
    activity = context.activity
    conversation = activity.conversation
    conversation_type = conversation.conversation_type if conversation is not None else None

    if conversation_type != "personal":
        await context.send_activity(
            "Sorry, I only chat in personal conversations right now."
        )
        return

    conversation_id = conversation.id
    await runtime.recipients.capture(context, "message")

    user_text = (activity.text or "").strip()

    if user_text.lower() == "cancel":
        cancelled_task_id = await runtime.work.cancel(conversation_id)
        if cancelled_task_id == "none":
            await context.send_activity("There is no active task to cancel.")
        else:
            await context.send_activity(f"Cancelled task {cancelled_task_id}.")
        return

    # The authenticated connector (needed to fetch Copilot inline images) lives
    # on the turn state.
    connector = None
    try:
        connector = context.turn_state.get("ConnectorClient")
    except Exception:  # pylint: disable=broad-exception-caught
        connector = None

    shared_files = await download_shared_files(
        activity, conversation_id, on_progress=None, connector=connector
    )
    if shared_files:
        names = ", ".join(f["name"] for f in shared_files)
        prompt = user_text or f"Please read the shared file(s) ({names}) and give me the key insights and a short summary."
    else:
        prompt = user_text

    work_input = long_running.WorkInput(
        conversation_id=conversation_id,
        prompt=prompt,
        files=shared_files or None,
        call_id=get_request_context().call_id,
    )

    result = await runtime.work.start(conversation_id, work_input)

    if result.status == "busy":
        await context.send_activity(
            f"I'm still working on your last request (task {result.task_id}). "
            "Say 'cancel' if you'd like to stop it."
        )
        return

    stream = context.streaming_response
    broker = result.broker

    async def _wait_for_event(timeout):
        if timeout is None:
            return await broker.next_event()
        try:
            return await asyncio.wait_for(broker.next_event(), timeout)
        except asyncio.TimeoutError:
            return None

    async def _on_cancel() -> None:
        await runtime.work.cancel(conversation_id)

    async def _on_stream_complete() -> None:
        await _deliver_ui(context, conversation_id, stream)

    async def _send_deferred(text: str) -> None:
        await context.send_activity(text)

    await long_running.consume_activity_turn(
        broker,
        stream,
        _wait_for_event,
        _on_cancel,
        on_stream_complete=_on_stream_complete,
        send_deferred=_send_deferred,
    )


# ── generic Invocations (notification broadcast) ────────────────────────────


async def handle_notification_invocation(payload: dict, runtime: AppRuntime) -> dict:
    """Validate a generic notification Invocations payload and broadcast it."""
    effective_payload = payload.get("input") if set(payload) == {"input"} else payload
    notification = invocations.NotificationInvocationPayload.model_validate(
        effective_payload
    )
    result = await runtime.recipients.broadcast(notification.notification)
    return {"sent": result.sent, "failed": result.failed, "pruned": result.pruned}


@host.invoke_handler
async def on_notification_invocation(request):
    """POST /invocations -- generic notification fan-out."""
    try:
        payload = await request.json()
        runtime = await _get_runtime()
        result = await handle_notification_invocation(payload, runtime)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("invalid notification invocation payload: %s", exc)
        return JSONResponse({"error": "invalid notification payload"}, status_code=400)
    return JSONResponse(result)


# ── M365 Activity handlers ───────────────────────────────────────────────────


@app.activity("message")
async def on_message(context, _state):
    await handle_message(context, await _get_runtime())


@app.activity("installationUpdate")
async def on_installation_update(context, _state):
    await handle_installation_update(context, await _get_runtime())


@app.activity("invoke")
async def on_invoke(context, _state):
    """Handle invoke activities — file-consent responses and card actions."""
    activity = context.activity
    name = getattr(activity, "name", None)

    # Teams file-consent (outbound file download).
    if outfiles.is_file_consent_invoke(activity):
        try:
            await outfiles.handle_file_consent_invoke(context)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("file consent handling failed: %s", exc, exc_info=True)
        return

    # Adaptive Card Universal Action (task board buttons).
    if name == cards.ADAPTIVE_ACTION_INVOKE:
        conversation_id = activity.conversation.id if activity.conversation else "unknown"
        parsed = cards.parse_action(activity)
        if parsed:
            verb, data = parsed
            task_id = data.get("taskId")
            try:
                if verb == "complete_task" and task_id:
                    _tools.complete_task_direct(conversation_id, task_id)
                elif verb == "delete_task" and task_id:
                    _tools.delete_task_direct(conversation_id, task_id)
                elif verb == "add_task":
                    # New task text comes from the card's Input.Text ('newTask').
                    new_title = (data.get("newTask") or "").strip()
                    if new_title:
                        _tools.add_task_direct(conversation_id, new_title)
                # refresh_tasks falls through to just re-render.
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("card action failed: %s", exc, exc_info=True)

            # Return a refreshed card as an IN-PLACE update via the invoke
            # response. Do NOT send a new card message — that would re-render a
            # card and (if it had an auto-refresh) loop. The invoke response
            # replaces the existing card in the chat.
            tasks = _tools.get_tasks(conversation_id)
            card = cards.build_tasks_card(conversation_id, tasks)
            resp = cards.card_invoke_response(card)
            try:
                from microsoft_agents.activity import Activity, ActivityTypes, InvokeResponse
                await context.send_activity(Activity(
                    type=ActivityTypes.invoke_response,
                    value=InvokeResponse(status=200, body=resp),
                ))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("could not send invoke response: %s", exc)
        return

    return


@app.error
async def on_error(context, error):
    """Handle unhandled errors."""
    logger.error("Handler error | error=%s", error, exc_info=True)
    try:
        await context.send_activity("Sorry, something went wrong. Please try again.")
    except Exception:  # pylint: disable=broad-exception-caught
        pass


@host.shutdown_handler
async def _on_host_shutdown() -> None:
    """Best-effort clean shutdown of the process-wide runtime, if built."""
    if _runtime is not None:
        await _shutdown_runtime(_runtime)


if __name__ == "__main__":
    print("Starting the GitHub Copilot SDK agent ...")
    host.run()
