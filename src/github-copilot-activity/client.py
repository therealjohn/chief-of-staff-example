# Copyright (c) Microsoft. All rights reserved.
"""GitHub Copilot SDK harness for the agent.

"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from azure.identity import DefaultAzureCredential
from copilot import SessionEventType

from copilot_sessions import SessionManager
from toolbox_runtime import ToolboxRuntime

logger = logging.getLogger("github-copilot.client")

_FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
_AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
_MODEL = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "")
_TOOLBOX_ENDPOINT = os.environ.get("TOOLBOX_ENDPOINT", "")
_INSTANCE_CLIENT_ID = os.environ.get("FOUNDRY_AGENT_INSTANCE_CLIENT_ID", "").strip()
_ENABLE_CODE_TOOLS = os.environ.get("COPILOT_ENABLE_CODE_TOOLS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


def resolve_model_provider(
    foundry_project_endpoint: str,
    azure_openai_endpoint: str,
) -> tuple[str, str]:
    """Choose the model endpoint and matching Microsoft Entra token scope."""
    if azure_openai_endpoint.strip():
        return (
            azure_openai_endpoint.strip(),
            "https://cognitiveservices.azure.com/.default",
        )
    return foundry_project_endpoint.strip(), "https://ai.azure.com/.default"


def build_credential(
    instance_client_id: str,
    *,
    factory: Callable[..., Any] = DefaultAzureCredential,
) -> Any:
    """Select the hosted agent identity while retaining local credential fallback."""
    kwargs = (
        {"managed_identity_client_id": instance_client_id}
        if instance_client_id
        else {}
    )
    return factory(**kwargs)

_MODEL_ENDPOINT, _MODEL_TOKEN_SCOPE = resolve_model_provider(
    _FOUNDRY_PROJECT_ENDPOINT,
    _AZURE_OPENAI_ENDPOINT,
)

# One shared credential, toolbox runtime, and session manager for the whole
# process -- the session manager owns the shared CopilotClient, the
# per-conversation session cache/locks, and the toolbox (MCP) connection
# lifecycle layered on top of the upstream tools.
_credential = build_credential(_INSTANCE_CLIENT_ID)
_toolbox_runtime = ToolboxRuntime(_TOOLBOX_ENDPOINT, _credential)
_session_manager = SessionManager(
    _MODEL_ENDPOINT,
    _MODEL,
    _credential,
    _toolbox_runtime,
    token_scope=_MODEL_TOKEN_SCOPE,
    enable_code_tools=_ENABLE_CODE_TOOLS,
)

# Generic, user-facing failure message: never embeds raw exception detail
# (fail closed). Detail is always logged instead.
_GENERIC_FAILURE_MESSAGE = "Sorry, something went wrong. Please try again."

# Friendly progress labels for the built-in / custom tools, shown to the user as
# transient "informative updates" while the model works (they vanish on the final
# streamed reply).
_TOOL_LABELS = {
    "add_task": "Adding your task…",
    "list_tasks": "Looking up your tasks…",
    "complete_task": "Marking the task done…",
    # built-in file tools the model uses to read shared files
    "view": "Reading the file…",
    "read_file": "Reading the file…",
    "bash": "Working with the file…",
    "grep": "Searching the file…",
    "glob": "Looking through the files…",
    "str_replace": "Editing the file…",
}


def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, f"Using {name.replace('_', ' ')}…")


async def ask_stream(conversation_id: str, text: str, files: list[dict[str, str]] | None = None):
    """Drive one turn and yield ``(kind, text)`` tuples as the model works.

    ``files`` is an optional list of ``{name, path}`` raw files to hand to the
    model as attachments — it reads/analyzes them itself (any type).

    ``kind`` is one of:
      - ``"progress"`` — a transient status line (tool activity); show + replace.
      - ``"delta"``    — an incremental chunk of the assistant's reply text.
      - ``"final"``    — the whole reply (only emitted when no deltas streamed,
                         e.g. an error string).
    """
    async with _session_manager.lock_for(conversation_id):  # one turn at a time
        try:
            session = await _session_manager.get(conversation_id)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            logger.error("ask_stream setup failed: %s", ex, exc_info=True)
            yield ("final", _GENERIC_FAILURE_MESSAGE)
            return

        attachments = []
        for f in (files or []):
            path = f.get("path")
            if not path:
                continue
            if f.get("kind") == "image" and f.get("mime"):
                # Inline image → base64 blob so the model can see it (vision).
                try:
                    import base64
                    with open(path, "rb") as fh:
                        data = base64.b64encode(fh.read()).decode("ascii")
                    attachments.append({
                        "type": "blob",
                        "data": data,
                        "mimeType": f["mime"],
                        "displayName": f.get("name", ""),
                    })
                except Exception as ex:  # pylint: disable=broad-exception-caught
                    logger.warning("could not encode image %s: %s", path, ex)
            else:
                attachments.append({"type": "file", "path": path, "displayName": f.get("name", "")})

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _on_event(ev):
            # May be invoked from the SDK's reader thread; hop to our loop safely.
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        unsubscribe = session.on(_on_event)
        got_delta = False
        emitted_terminal_final = False
        final_text = ""
        try:
            await session.send(text, attachments=attachments or None)  # dispatch the turn
            while True:
                ev = await asyncio.wait_for(queue.get(), timeout=90)
                etype = ev.type
                data = ev.data
                if etype == SessionEventType.TOOL_EXECUTION_START:
                    yield ("progress", _tool_label(getattr(data, "tool_name", "") or ""))
                elif etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                    chunk = getattr(data, "delta_content", "") or ""
                    if chunk:
                        got_delta = True
                        yield ("delta", chunk)
                elif etype == SessionEventType.ASSISTANT_MESSAGE:
                    final_text = getattr(data, "content", "") or final_text
                elif etype in (SessionEventType.SESSION_IDLE, SessionEventType.ASSISTANT_IDLE):
                    break
                elif etype == SessionEventType.SESSION_ERROR:
                    logger.error("session error event: %s", getattr(data, "__dict__", data))
                    await _session_manager.reset(conversation_id)
                    if not got_delta:
                        yield ("final", "Sorry, I hit a problem answering that.")
                        emitted_terminal_final = True
                    break
        except asyncio.TimeoutError:
            logger.warning("ask_stream timed out; resetting session")
            await _session_manager.reset(conversation_id)
            if not got_delta:
                yield ("final", "Sorry, that took too long. Please try again.")
            return
        except Exception as ex:  # pylint: disable=broad-exception-caught
            logger.error("ask_stream failed: %s", ex, exc_info=True)
            await _session_manager.reset(conversation_id)
            if not got_delta:
                yield ("final", _GENERIC_FAILURE_MESSAGE)
            return
        finally:
            try:
                unsubscribe()
            except Exception:  # pylint: disable=broad-exception-caught
                pass

        if not got_delta and not emitted_terminal_final:
            yield ("final", final_text.strip() or "(no response)")


async def abort_turn(conversation_id: str) -> None:
    """Abort the in-flight turn for ``conversation_id``, if any."""
    await _session_manager.abort_turn(conversation_id)


async def close() -> None:
    """Reset every cached conversation session and stop the shared client."""
    await _session_manager.close()
