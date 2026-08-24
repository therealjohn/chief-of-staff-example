"""Tests for client.py's adaptation onto copilot_sessions.SessionManager.

These tests describe the desired post-refactor behaviour of ``client.py``
(issue-driven RED phase -- no production code has been changed yet):

  - Module-level ``_session_manager`` is a ``copilot_sessions.SessionManager``
    built from the configured endpoint/model/credential/toolbox runtime.
  - ``ask_stream(conversation_id, text, files=None)`` takes the turn under
    ``_session_manager.lock_for(conversation_id)``, fetches the session via
    ``await _session_manager.get(conversation_id)``, and maps Copilot SDK
    events to ``(kind, text)`` tuples exactly as before (tool start ->
    "progress", assistant delta -> "delta", no deltas -> "final").
  - On a per-event 90s timeout, it resets the conversation's session and
    yields the existing timeout message only if no delta was emitted yet.
  - On any other turn failure, it resets the conversation's session and
    yields a generic, user-facing failure that never embeds the raw
    exception text (fail closed) -- detail stays in the logs only.
  - Public ``await abort_turn(conversation_id)`` / ``await close()`` delegate
    to the manager's ``abort_turn`` / ``close``.

All fakes below are complete, hand-written async doubles -- no mock
framework, no real Copilot process, no sleeps.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from copilot import SessionEventType  # type: ignore[import-not-found]

import client  # type: ignore[import-not-found]
from copilot_sessions import SessionManager  # type: ignore[import-not-found]

_CONV = "conv-1"


# ── complete, hand-written fakes (no mock framework, no network, no sleeps) ─


def _event(event_type, **data_fields):
    """Build a minimal Copilot SDK event: ``.type`` + ``.data.<field>``."""
    return SimpleNamespace(type=event_type, data=SimpleNamespace(**data_fields))


class _FakeCopilotSession:
    """Complete async double for the SDK's per-conversation session.

    ``send`` synchronously fires every scripted event at the subscribed
    callback (mirroring the real SDK dispatching events before ``send``
    resolves), or raises ``send_error`` instead if one was given.
    """

    def __init__(self, events=(), send_error=None):
        self._events = list(events)
        self._send_error = send_error
        self._callback = None
        self.send_calls: list[tuple[str, object]] = []
        self.aborted = False

    def on(self, callback):
        self._callback = callback

        def _unsubscribe():
            self._callback = None

        return _unsubscribe

    async def send(self, text, attachments=None):
        self.send_calls.append((text, attachments))
        if self._send_error is not None:
            raise self._send_error
        for ev in self._events:
            self._callback(ev)

    async def abort(self):
        self.aborted = True


class _FakeSessionManager:
    """Complete async double for ``copilot_sessions.SessionManager``."""

    def __init__(self, session):
        self._session = session
        self._locks: dict[str, asyncio.Lock] = {}
        self.lock_for_calls: list[str] = []
        self.get_calls: list[str] = []
        self.reset_calls: list[str] = []
        self.abort_turn_calls: list[str] = []
        self.close_calls = 0

    def lock_for(self, conversation_id):
        self.lock_for_calls.append(conversation_id)
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    async def get(self, conversation_id):
        self.get_calls.append(conversation_id)
        return self._session

    async def reset(self, conversation_id):
        self.reset_calls.append(conversation_id)

    async def abort_turn(self, conversation_id):
        self.abort_turn_calls.append(conversation_id)

    async def close(self):
        self.close_calls += 1


async def _collect(agen):
    return [item async for item in agen]


def _install_fake_manager(monkeypatch, events=(), send_error=None):
    session = _FakeCopilotSession(events=events, send_error=send_error)
    manager = _FakeSessionManager(session)
    monkeypatch.setattr(client, "_session_manager", manager, raising=False)
    return manager, session


# ── module-level _session_manager wiring ────────────────────────────────────


def test_resolve_model_provider_prefers_azure_openai_endpoint():
    endpoint, scope = client.resolve_model_provider(
        "https://account.services.ai.azure.com/api/projects/project",
        "https://account.openai.azure.com/",
    )

    assert endpoint == "https://account.openai.azure.com/"
    assert scope == "https://cognitiveservices.azure.com/.default"


def test_resolve_model_provider_falls_back_to_foundry_project_endpoint():
    endpoint, scope = client.resolve_model_provider(
        "https://account.services.ai.azure.com/api/projects/project",
        "",
    )

    assert endpoint == "https://account.services.ai.azure.com/api/projects/project"
    assert scope == "https://ai.azure.com/.default"


def test_build_credential_selects_the_hosted_agent_instance_identity():
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return object()

    client.build_credential("instance-client-id", factory=factory)

    assert calls == [{"managed_identity_client_id": "instance-client-id"}]


def test_build_credential_uses_default_chain_locally():
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return object()

    client.build_credential("", factory=factory)

    assert calls == [{}]


def test_module_level_session_manager_is_a_session_manager_instance():
    assert isinstance(client._session_manager, SessionManager)


# ── ask_stream(): uses the manager's lock + get for the conversation ────────


@pytest.mark.asyncio
async def test_ask_stream_uses_manager_lock_for_and_get_for_the_conversation(monkeypatch):
    manager, session = _install_fake_manager(
        monkeypatch, events=[_event(SessionEventType.SESSION_IDLE)]
    )

    await _collect(client.ask_stream(_CONV, "hello?"))

    assert manager.lock_for_calls == [_CONV]
    assert manager.get_calls == [_CONV]
    assert session.send_calls == [("hello?", None)]


# ── ask_stream(): tool start -> progress, assistant delta -> delta ─────────


@pytest.mark.asyncio
async def test_ask_stream_maps_tool_start_to_progress_and_assistant_delta_to_delta(monkeypatch):
    events = [
        _event(SessionEventType.TOOL_EXECUTION_START, tool_name="add_task"),
        _event(SessionEventType.ASSISTANT_MESSAGE_DELTA, delta_content="Hello"),
        _event(SessionEventType.ASSISTANT_MESSAGE_DELTA, delta_content=" world"),
        _event(SessionEventType.SESSION_IDLE),
    ]
    _install_fake_manager(monkeypatch, events=events)

    results = await _collect(client.ask_stream(_CONV, "add a task"))

    assert results == [
        ("progress", client._tool_label("add_task")),
        ("delta", "Hello"),
        ("delta", " world"),
    ]


# ── ask_stream(): no deltas -> single final assembled from the message ─────


@pytest.mark.asyncio
async def test_ask_stream_yields_final_text_when_no_deltas_streamed(monkeypatch):
    events = [
        _event(SessionEventType.TOOL_EXECUTION_START, tool_name="list_tasks"),
        _event(SessionEventType.ASSISTANT_MESSAGE, content="All done."),
        _event(SessionEventType.SESSION_IDLE),
    ]
    _install_fake_manager(monkeypatch, events=events)

    results = await _collect(client.ask_stream(_CONV, "what are my tasks?"))

    assert results == [
        ("progress", client._tool_label("list_tasks")),
        ("final", "All done."),
    ]


@pytest.mark.asyncio
async def test_ask_stream_yields_only_one_failure_for_a_session_error_event(monkeypatch):
    events = [
        _event(SessionEventType.SESSION_ERROR, message="provider failed"),
        _event(SessionEventType.SESSION_IDLE),
    ]
    manager, _session = _install_fake_manager(monkeypatch, events=events)

    results = await _collect(client.ask_stream(_CONV, "hello?"))

    assert manager.reset_calls == [_CONV]
    assert results == [("final", "Sorry, I hit a problem answering that.")]


# ── ask_stream(): per-event timeout -> reset + existing timeout message ────


@pytest.mark.asyncio
async def test_ask_stream_resets_manager_and_yields_timeout_message_when_no_delta_emitted(
    monkeypatch,
):
    manager, _session = _install_fake_manager(monkeypatch, events=[])  # nothing ever arrives

    async def _always_times_out(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _always_times_out)

    results = await _collect(client.ask_stream(_CONV, "hello?"))

    assert manager.reset_calls == [_CONV]
    assert results == [("final", "Sorry, that took too long. Please try again.")]


@pytest.mark.asyncio
async def test_ask_stream_does_not_yield_timeout_message_when_a_delta_already_streamed(
    monkeypatch,
):
    events = [_event(SessionEventType.ASSISTANT_MESSAGE_DELTA, delta_content="partial")]
    manager, _session = _install_fake_manager(monkeypatch, events=events)

    real_wait_for = asyncio.wait_for
    remaining_real_calls = 1  # let the queued delta through, then time out

    async def _times_out_after_first_call(coro, timeout):
        nonlocal remaining_real_calls
        if remaining_real_calls > 0:
            remaining_real_calls -= 1
            return await real_wait_for(coro, timeout)
        coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _times_out_after_first_call)

    results = await _collect(client.ask_stream(_CONV, "hello?"))

    assert manager.reset_calls == [_CONV]
    assert results == [("delta", "partial")]  # no extra "final" once a delta streamed


# ── ask_stream(): other turn failure -> reset + generic, non-leaking failure ─


@pytest.mark.asyncio
async def test_ask_stream_resets_manager_and_yields_generic_failure_without_raw_exception_detail(
    monkeypatch,
):
    secret_detail = "upstream auth failed: api_key=sk-super-secret-12345"
    manager, _session = _install_fake_manager(
        monkeypatch, send_error=RuntimeError(secret_detail)
    )

    results = await _collect(client.ask_stream(_CONV, "hello?"))

    assert manager.reset_calls == [_CONV]
    assert len(results) == 1
    kind, text = results[0]
    assert kind == "final"
    assert text  # some non-empty, user-facing message
    assert secret_detail not in text  # fail closed: never leak the raw exception


# ── abort_turn() / close(): delegate to the session manager ─────────────────


@pytest.mark.asyncio
async def test_abort_turn_delegates_to_session_manager_abort_turn(monkeypatch):
    manager, _session = _install_fake_manager(monkeypatch)

    await client.abort_turn(_CONV)

    assert manager.abort_turn_calls == [_CONV]


@pytest.mark.asyncio
async def test_close_delegates_to_session_manager_close(monkeypatch):
    manager, _session = _install_fake_manager(monkeypatch)

    await client.close()

    assert manager.close_calls == 1
