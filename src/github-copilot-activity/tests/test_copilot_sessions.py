"""Tests for the per-conversation Copilot session lifecycle manager.

Expected (not yet implemented) production module: ``copilot_sessions``

  - ``build_system_message(workiq_available: bool) -> str``
      A "Chief of Staff" persona system prompt. Retains the upstream
      to-do-list / shared-file-reading / document-generation tool-calling
      behavior (see ``client.py``'s current system message). It leaves
      workflow selection to model-invoked Skill metadata rather than
      duplicating intent routing in the base prompt. When
      ``workiq_available`` is ``False`` it must explicitly say M365 data is
      unavailable and that M365-dependent requests must fail closed
      (general chat still works). Always states the safe-write boundary
      (reads / drafts / conditional writes only; never unattended sends or
      deletes -- mirrors ``workiq_policy``).

  - ``SessionManager(endpoint, model, credential, toolbox_runtime, *,
    client_factory=CopilotClient, base_tools_builder=tools.build_tools,
    system_message_builder=build_system_message)``

      * ``lock_for(conversation_id) -> asyncio.Lock`` -- the same lock
        instance for the same conversation id, a different instance for a
        different conversation id.

      * ``await get(conversation_id)`` -- validates endpoint/model; lazily
        creates and starts exactly one ``CopilotClient`` (shared across
        conversations); fetches a fresh ``https://ai.azure.com/.default``
        token; reuses the cached entry when the token is unchanged,
        otherwise builds a brand new entry and replaces the cache,
        cleaning up the old session/toolbox connection.
          - toolbox connect succeeds -> its ``.tools`` are appended to
            ``base_tools_builder(conversation_id)``, ``workiq_available``
            is ``True``, and the ``ToolboxSession`` is retained for later
            cleanup.
          - toolbox connect raises ``ToolboxUnavailableError`` (or returns
            ``None``) -> falls back to upstream tools only,
            ``workiq_available`` is ``False`` (never a success-shaped
            claim; general chat fallback).
        Builds real session options: an azure ``ProviderConfig``
        (``wire_api="responses"``, the endpoint as ``base_url``, a bearer
        token), the configured model, the combined tools,
        ``ToolSet().add_builtin("*").add_custom("*")``, a replaced system
        message built from ``system_message_builder(workiq_available)``, the
        service-local Skills directory enabled, ``on_permission_request=
        PermissionHandler.approve_all``, and ``streaming=True``.
        Prefers ``client.resume_session(session_id_for(conversation_id),
        **opts)``; if that raises, falls back to
        ``client.create_session(session_id=..., **opts)``.
        On token rotation: aborts the old session, closes the old toolbox
        session, then replaces the cache entry.

      * ``session_id_for(conversation_id) -> str`` -- stable (same input ->
        same output), <=64 chars, distinct per conversation id (public form
        of the upstream hashed-session-id behavior).

      * ``await abort_turn(conversation_id)`` -- aborts the cached session
        but keeps the cache entry and toolbox connection intact.

      * ``await reset(conversation_id)`` -- drops the cache entry, aborts
        the session, closes the toolbox session, and removes the
        conversation's lock. Propagates failures rather than swallowing
        them.

      * ``await close()`` -- resets every cached conversation and stops the
        client if (and only if) it exposes an async ``stop()``.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from azure.core.credentials import AccessToken
from azure.ai.agentserver.core import (
    FoundryAgentRequestContext,
    reset_request_context,
    set_request_context,
)
from copilot import PermissionHandler, ProviderConfig

from toolbox_runtime import ToolboxUnavailableError  # type: ignore[import-not-found]

from copilot_sessions import (  # type: ignore[import-not-found]
    SessionManager,
    build_system_message,
)

_ENDPOINT = "https://example.foundry.test"
_MODEL = "gpt-test"
_BRIEFING_SKILL_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "chief-of-staff-briefing"
    / "SKILL.md"
)


def _briefing_skill_text() -> str:
    return _BRIEFING_SKILL_PATH.read_text(encoding="utf-8")


# ── Complete, hand-written fakes (no mock framework, no network, no sleeps) ──


class _FakeCredential:
    """Complete double for ``DefaultAzureCredential`` using the real ``AccessToken``."""

    def __init__(self, tokens):
        self._tokens = list(tokens)
        self.calls: list[str] = []

    def get_token(self, scope):
        self.calls.append(scope)
        value = self._tokens.pop(0) if len(self._tokens) > 1 else self._tokens[0]
        return AccessToken(value, 9999999999)


class _FakeSession:
    """Complete async double for the SDK's ``CopilotSession``."""

    def __init__(self, session_id, opts):
        self.session_id = session_id
        self.opts = opts
        self.aborted = False

    async def abort(self):
        self.aborted = True


class _FakeClient:
    """Complete async double for ``CopilotClient`` (with ``stop``)."""

    def __init__(self, calls, *, resume_error=None):
        self._calls = calls
        self._resume_error = resume_error
        self.started = False
        self.stopped = False

    async def start(self):
        self._calls.append("start")
        self.started = True

    async def resume_session(self, session_id, **opts):
        self._calls.append(("resume_session", session_id))
        if self._resume_error is not None:
            raise self._resume_error
        return _FakeSession(session_id, opts)

    async def create_session(self, *, session_id, **opts):
        self._calls.append(("create_session", session_id))
        return _FakeSession(session_id, opts)

    async def stop(self):
        self._calls.append("stop")
        self.stopped = True


class _FakeClientNoStop:
    """Complete async double for ``CopilotClient`` that has no ``stop`` at all."""

    def __init__(self, calls):
        self._calls = calls

    async def start(self):
        self._calls.append("start")

    async def resume_session(self, session_id, **opts):
        self._calls.append(("resume_session", session_id))
        return _FakeSession(session_id, opts)

    async def create_session(self, *, session_id, **opts):
        self._calls.append(("create_session", session_id))
        return _FakeSession(session_id, opts)


class _FakeToolboxSession:
    """Complete async double for ``toolbox_runtime.ToolboxSession``."""

    def __init__(self, calls, tools):
        self._calls = calls
        self.tools = tools
        self.closed = False

    async def close(self):
        self._calls.append("toolbox_close")
        self.closed = True


class _FakeToolboxRuntime:
    """Complete async double for ``toolbox_runtime.ToolboxRuntime``.

    Hands out ``sessions`` in order (one per successful ``connect()``),
    repeating the last one once exhausted; or raises ``error`` if given.
    """

    def __init__(self, calls, *, sessions=None, error=None):
        self._calls = calls
        self._sessions = list(sessions) if sessions is not None else []
        self._error = error
        self.connect_count = 0
        self.platform_headers: list[object] = []

    async def connect(self, *, platform_headers=None):
        self._calls.append("toolbox_connect")
        self.connect_count += 1
        self.platform_headers.append(platform_headers)
        if self._error is not None:
            raise self._error
        if not self._sessions:
            return None
        if len(self._sessions) > 1:
            return self._sessions.pop(0)
        return self._sessions[0]


@dataclass
class _Fixture:
    manager: SessionManager
    calls: list
    client: object
    credential: object
    toolbox_runtime: object
    base_tools_calls: list = field(default_factory=list)
    system_message_calls: list = field(default_factory=list)


def _make_manager(*, endpoint=_ENDPOINT, model=_MODEL, credential=None,
                   toolbox_runtime=None, client=None, calls=None,
                   enable_code_tools=False):
    calls = calls if calls is not None else []
    credential = credential if credential is not None else _FakeCredential(["token-a"])
    toolbox_runtime = (
        toolbox_runtime if toolbox_runtime is not None else _FakeToolboxRuntime(calls)
    )
    client = client if client is not None else _FakeClient(calls)

    def client_factory():
        calls.append("client_factory")
        return client

    base_tools_calls: list = []

    def base_tools_builder(conversation_id):
        calls.append(("base_tools_builder", conversation_id))
        base_tools_calls.append(conversation_id)
        return ["upstream-tool"]

    system_message_calls: list = []

    def system_message_builder(workiq_available):
        calls.append(("system_message_builder", workiq_available))
        system_message_calls.append(workiq_available)
        return f"SYSTEM[{workiq_available}]"

    manager = SessionManager(
        endpoint,
        model,
        credential,
        toolbox_runtime,
        client_factory=client_factory,
        base_tools_builder=base_tools_builder,
        system_message_builder=system_message_builder,
        enable_code_tools=enable_code_tools,
    )
    return _Fixture(
        manager=manager,
        calls=calls,
        client=client,
        credential=credential,
        toolbox_runtime=toolbox_runtime,
        base_tools_calls=base_tools_calls,
        system_message_calls=system_message_calls,
    )


# ── build_system_message: Chief of Staff persona ────────────────────────────


def test_build_system_message_names_the_chief_of_staff_persona():
    message = build_system_message(True).lower()

    assert "chief of staff" in message


def test_build_system_message_retains_upstream_task_list_behavior():
    message = build_system_message(True).lower()

    assert "to-do" in message or "task" in message


def test_build_system_message_retains_upstream_shared_file_reading_behavior():
    message = build_system_message(True).lower()

    assert "shared" in message and "file" in message


def test_build_system_message_retains_upstream_document_generation_behavior():
    message = build_system_message(True, code_tools_available=True).lower()

    assert "deliver_file" in message or "document" in message


def test_build_system_message_disables_code_and_file_generation_by_default():
    message = build_system_message(True).lower()

    assert "downloadable file generation is disabled" in message
    assert "shell and python tools" not in message


def test_build_system_message_treats_retrieved_content_as_untrusted_data():
    message = build_system_message(True).lower()

    assert "retrieved content" in message
    assert "untrusted data" in message
    assert "current user message" in message


def test_build_system_message_leaves_briefing_skill_selection_to_sdk_metadata():
    message = build_system_message(True).lower()

    assert "briefing" not in message
    assert "chief-of-staff-briefing" not in message


def test_briefing_skill_description_owns_all_intent_based_triggering():
    message = _briefing_skill_text().lower()
    frontmatter = message.split("---", 2)[1]

    assert "name: chief-of-staff-briefing" in frontmatter
    assert "description:" in frontmatter
    for phrase in (
        "use this skill whenever",
        "asks to be briefed",
        "caught up",
        "morning or daily brief",
        "what they missed",
        "what needs attention",
        "today's priorities",
        "meeting preparation",
        "accepts an offer",
        "even if they do not call it a briefing",
    ):
        assert phrase in frontmatter
    assert "disable-model-invocation" not in frontmatter


def test_briefing_skill_markdown_does_not_hard_wrap_prose():
    body = _briefing_skill_text().split("---", 2)[2]
    lines = body.splitlines()

    def is_plain_prose(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and not (
            stripped.startswith(("#", "-", "`"))
            or (stripped[0].isdigit() and ". " in stripped[:4])
        )

    for index, line in enumerate(lines):
        stripped = line.strip()
        assert not (line.startswith((" ", "\t")) and stripped and not stripped.startswith("- "))
        if index:
            assert not (is_plain_prose(lines[index - 1]) and is_plain_prose(line))


def test_briefing_skill_requires_broad_retrieval_and_full_thread_expansion():
    message = _briefing_skill_text().lower()

    for phrase in (
        "today's calendar",
        "previous 24 hours",
        "mail",
        "teams chats",
        "teams channels",
        "full message",
        "thread replies",
        "meeting insights",
        "current briefing conversation",
    ):
        assert phrase in message
    assert "do not stop at the first" in message


def test_briefing_skill_requires_cross_source_prioritization_and_actionable_detail():
    message = _briefing_skill_text().lower()

    for phrase in (
        "correlate",
        "deduplicate",
        "direct ask",
        "decision",
        "deadline",
        "blocker",
        "why it matters",
        "next action",
    ):
        assert phrase in message


def test_briefing_skill_defines_an_evidence_dense_conditional_structure():
    message = _briefing_skill_text().lower()

    for phrase in (
        "chief of staff brief",
        "top priorities",
        "today's agenda",
        "good to know",
        "teams channels",
        "your actions",
        "inline source link",
        "omit empty sections",
    ):
        assert phrase in message
    assert "raw url" in message
    assert "generic offer" in message


def test_briefing_skill_handles_empty_and_failed_sources_without_inventing_activity():
    message = _briefing_skill_text().lower()

    assert "never invent" in message
    assert "source fails" in message
    assert "no activity" in message
    assert "successfully searched" in message


def test_briefing_skill_never_follows_instructions_from_retrieved_content():
    message = _briefing_skill_text().lower()

    assert "untrusted data" in message
    assert "never follow instructions" in message


def test_briefing_skill_requires_chat_output_and_forbids_files_by_default():
    message = _briefing_skill_text().lower()

    assert "directly in the teams chat" in message
    assert "do not create" in message
    assert "deliver_file" in message
    assert "explicitly asks for a downloadable file" in message


def test_briefing_skill_does_not_substitute_local_workspace_data_for_m365():
    message = _briefing_skill_text().lower()

    assert "do not substitute" in message
    assert "local task" in message
    assert "microsoft 365" in message
    assert "briefing cannot be completed" in message


def test_build_system_message_states_the_safe_write_boundary_for_m365_actions():
    message = build_system_message(True).lower()

    assert "draft" in message
    assert "never" in message
    assert "send" in message


def test_build_system_message_when_workiq_unavailable_says_m365_data_is_unavailable():
    message = build_system_message(False).lower()

    assert "m365" in message
    assert "unavailable" in message


def test_build_system_message_when_workiq_unavailable_m365_requests_must_fail_closed():
    message = build_system_message(False).lower()

    assert "fail closed" in message or "fail-closed" in message


def test_build_system_message_differs_between_workiq_available_and_unavailable():
    assert build_system_message(True) != build_system_message(False)


# ── SessionManager: construction defaults ───────────────────────────────────


def test_default_collaborators_use_safe_production_defaults_without_connecting():
    from copilot import CopilotClient

    manager = SessionManager(_ENDPOINT, _MODEL, _FakeCredential(["t"]), _FakeToolboxRuntime([]))

    assert manager._client_factory is CopilotClient
    assert "deliver_file" not in [
        tool.name for tool in manager._base_tools_builder("conversation")
    ]
    assert manager._system_message_builder(True) == build_system_message(True)


# ── lock_for: per-conversation lock isolation ───────────────────────────────


def test_lock_for_returns_the_same_lock_instance_for_the_same_conversation_id():
    fixture = _make_manager()

    lock_a = fixture.manager.lock_for("conv-1")
    lock_b = fixture.manager.lock_for("conv-1")

    assert lock_a is lock_b


def test_lock_for_returns_a_different_lock_instance_for_a_different_conversation_id():
    fixture = _make_manager()

    lock_a = fixture.manager.lock_for("conv-1")
    lock_b = fixture.manager.lock_for("conv-2")

    assert lock_a is not lock_b
    assert isinstance(lock_a, asyncio.Lock)


# ── get(): endpoint/model validation ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,model", [("", _MODEL), (_ENDPOINT, ""), ("   ", "   ")])
async def test_get_raises_when_endpoint_or_model_is_blank(endpoint, model):
    fixture = _make_manager(endpoint=endpoint, model=model)

    with pytest.raises(RuntimeError):
        await fixture.manager.get("conv-1")

    assert fixture.calls == []  # validated before creating anything


# ── get(): one shared, lazily-started client ─────────────────────────────────


@pytest.mark.asyncio
async def test_get_creates_and_starts_exactly_one_client_shared_across_conversations():
    fixture = _make_manager()

    await fixture.manager.get("conv-1")
    await fixture.manager.get("conv-2")

    assert fixture.calls.count("client_factory") == 1
    assert fixture.client.started is True


# ── get(): toolbox success / unavailable ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_appends_toolbox_tools_and_marks_workiq_available_on_toolbox_success():
    calls: list = []
    toolbox_session = _FakeToolboxSession(calls, tools=["toolbox-tool"])
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=[toolbox_session])
    fixture = _make_manager(toolbox_runtime=toolbox_runtime, calls=calls)

    session = await fixture.manager.get("conv-1")

    assert fixture.base_tools_calls == ["conv-1"]
    assert session.opts["tools"] == ["upstream-tool", "toolbox-tool"]
    assert fixture.system_message_calls == [True]
    assert session.opts["system_message"] == {"mode": "replace", "content": "SYSTEM[True]"}


@pytest.mark.asyncio
async def test_get_falls_back_to_upstream_tools_only_when_toolbox_is_unavailable():
    calls: list = []
    toolbox_runtime = _FakeToolboxRuntime(
        calls,
        error=ToolboxUnavailableError("initialize", RuntimeError("down")),
    )
    fixture = _make_manager(toolbox_runtime=toolbox_runtime, calls=calls)

    session = await fixture.manager.get("conv-1")

    assert session.opts["tools"] == ["upstream-tool"]
    assert fixture.system_message_calls == [False]


@pytest.mark.asyncio
async def test_get_treats_a_none_toolbox_connect_result_as_unavailable_too():
    calls: list = []
    toolbox_runtime = _FakeToolboxRuntime(calls)  # no sessions, no error -> connect() -> None
    fixture = _make_manager(toolbox_runtime=toolbox_runtime, calls=calls)

    session = await fixture.manager.get("conv-1")

    assert session.opts["tools"] == ["upstream-tool"]
    assert fixture.system_message_calls == [False]


@pytest.mark.asyncio
async def test_get_retries_an_unavailable_toolbox_and_upgrades_the_cached_session():
    calls: list = []
    toolbox_session = _FakeToolboxSession(calls, tools=["toolbox-tool"])
    toolbox_runtime = _FakeToolboxRuntime(
        calls,
        sessions=[None, toolbox_session],
    )
    fixture = _make_manager(toolbox_runtime=toolbox_runtime, calls=calls)

    first_session = await fixture.manager.get("conv-1")
    second_session = await fixture.manager.get("conv-1")

    assert toolbox_runtime.connect_count == 2
    assert fixture.system_message_calls == [False, True]
    assert first_session.aborted is True
    assert second_session is not first_session
    assert second_session.opts["tools"] == ["upstream-tool", "toolbox-tool"]


# ── get(): real session options assembly ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_builds_the_azure_provider_config_toolset_and_permission_handler():
    fixture = _make_manager()

    session = await fixture.manager.get("conv-1")

    provider = session.opts["provider"]
    assert provider["type"] == "azure"
    assert provider["base_url"] == _ENDPOINT
    assert provider["wire_api"] == "responses"
    assert "bearer_token" not in provider
    token_result = provider["bearer_token_provider"](
        {"provider_name": "default", "session_id": "session-1"}
    )
    if inspect.isawaitable(token_result):
        token_result = await token_result
    assert token_result == "token-a"
    assert session.opts["model"] == _MODEL
    assert session.opts["available_tools"].to_list() == [
        "builtin:skill",
        "builtin:view",
        "builtin:grep",
        "builtin:glob",
        "custom:*",
    ]
    assert session.opts["on_permission_request"] is PermissionHandler.approve_all
    assert session.opts["streaming"] is True


@pytest.mark.asyncio
async def test_get_exposes_all_builtin_tools_only_when_code_tools_are_enabled():
    fixture = _make_manager(enable_code_tools=True)

    session = await fixture.manager.get("conv-1")

    assert session.opts["available_tools"].to_list() == ["builtin:*", "custom:*"]


@pytest.mark.asyncio
async def test_get_enables_the_service_local_skill_directory():
    fixture = _make_manager()

    session = await fixture.manager.get("conv-1")

    expected_directory = Path(__file__).parents[1] / "skills"
    assert session.opts["enable_skills"] is True
    assert session.opts["skill_directories"] == [str(expected_directory)]


@pytest.mark.asyncio
async def test_custom_model_token_scope_is_used_for_cache_and_provider_requests():
    calls: list = []
    credential = _FakeCredential(["token-a"])
    manager = SessionManager(
        _ENDPOINT,
        _MODEL,
        credential,
        _FakeToolboxRuntime(calls),
        client_factory=lambda: _FakeClient(calls),
        base_tools_builder=lambda _conversation_id: [],
        system_message_builder=lambda _available: "system",
        token_scope="https://cognitiveservices.azure.com/.default",
    )

    session = await manager.get("conv-1")
    provider = session.opts["provider"]
    provider["bearer_token_provider"](
        {"provider_name": "default", "session_id": "session-1"}
    )

    assert credential.calls == [
        "https://cognitiveservices.azure.com/.default",
        "https://cognitiveservices.azure.com/.default",
    ]


# ── get(): stable hashed session id + resume-preferred / create fallback ────


def test_session_id_for_is_stable_and_distinct_per_conversation_and_at_most_64_chars():
    fixture = _make_manager()

    a1 = fixture.manager.session_id_for("conv-1")
    a2 = fixture.manager.session_id_for("conv-1")
    b = fixture.manager.session_id_for("conv-2")

    assert a1 == a2
    assert a1 != b
    assert a1.startswith("conv-v1-")
    assert len(a1) <= 64


@pytest.mark.asyncio
async def test_get_prefers_resume_session_with_the_stable_hashed_session_id():
    fixture = _make_manager()

    session = await fixture.manager.get("conv-1")
    sid = fixture.manager.session_id_for("conv-1")

    assert session.session_id == sid
    assert ("resume_session", sid) in fixture.calls
    assert not any(
        call[0] == "create_session" for call in fixture.calls if isinstance(call, tuple)
    )


@pytest.mark.asyncio
async def test_get_falls_back_to_create_session_when_resume_session_raises():
    calls: list = []
    client = _FakeClient(calls, resume_error=RuntimeError("no such session"))
    fixture = _make_manager(client=client, calls=calls)

    session = await fixture.manager.get("conv-1")
    sid = fixture.manager.session_id_for("conv-1")

    assert ("resume_session", sid) in fixture.calls
    assert ("create_session", sid) in fixture.calls
    assert session.session_id == sid


@pytest.mark.asyncio
async def test_get_closes_new_toolbox_when_resume_and_create_both_fail():
    calls: list = []
    toolbox_session = _FakeToolboxSession(calls, tools=["toolbox-tool"])
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=[toolbox_session])

    class FailingClient(_FakeClient):
        async def create_session(self, *, session_id, **opts):
            self._calls.append(("create_session", session_id))
            raise RuntimeError("create failed")

    client = FailingClient(calls, resume_error=RuntimeError("resume failed"))
    fixture = _make_manager(
        client=client,
        toolbox_runtime=toolbox_runtime,
        calls=calls,
    )

    with pytest.raises(RuntimeError, match="create failed"):
        await fixture.manager.get("conv-1")

    assert toolbox_session.closed is True


@pytest.mark.asyncio
async def test_concurrent_conversations_wait_for_shared_client_start_to_finish():
    calls: list = []
    start_entered = asyncio.Event()
    release_start = asyncio.Event()

    class BlockingStartClient(_FakeClient):
        async def start(self):
            self._calls.append("start")
            start_entered.set()
            await release_start.wait()
            self.started = True

        async def resume_session(self, session_id, **opts):
            if not self.started:
                raise AssertionError("session used before client.start completed")
            return await super().resume_session(session_id, **opts)

    client = BlockingStartClient(calls)
    fixture = _make_manager(client=client, calls=calls)

    first = asyncio.create_task(fixture.manager.get("conv-1"))
    await start_entered.wait()
    second = asyncio.create_task(fixture.manager.get("conv-2"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert second.done() is False

    release_start.set()

    await asyncio.gather(first, second)

    assert calls.count("start") == 1


# ── get(): cache reuse vs. token rotation ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reuses_the_cached_session_when_the_token_is_unchanged():
    calls: list = []
    credential = _FakeCredential(["same-token"])
    toolbox_session = _FakeToolboxSession(calls, tools=["toolbox-tool"])
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=[toolbox_session])
    fixture = _make_manager(
        credential=credential,
        toolbox_runtime=toolbox_runtime,
        calls=calls,
    )

    first = await fixture.manager.get("conv-1")
    calls.clear()
    second = await fixture.manager.get("conv-1")

    assert second is first
    assert calls == []  # no reconnect / resume / create for an unchanged token


@pytest.mark.asyncio
async def test_cached_toolbox_session_refreshes_call_id_for_each_turn_and_cross_thread_tool_call():
    calls: list = []
    credential = _FakeCredential(["same-token"])
    toolbox_session = _FakeToolboxSession(calls, tools=["toolbox-tool"])
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=[toolbox_session])
    fixture = _make_manager(
        credential=credential,
        toolbox_runtime=toolbox_runtime,
        calls=calls,
    )

    first_token = set_request_context(FoundryAgentRequestContext(call_id="call-1"))
    try:
        await fixture.manager.get("conv-1")
    finally:
        reset_request_context(first_token)

    header_source = toolbox_runtime.platform_headers[0]
    assert await asyncio.to_thread(header_source) == {
        "x-agent-foundry-call-id": "call-1"
    }

    second_token = set_request_context(FoundryAgentRequestContext(call_id="call-2"))
    try:
        await fixture.manager.get("conv-1")
    finally:
        reset_request_context(second_token)

    assert toolbox_runtime.connect_count == 1
    assert await asyncio.to_thread(header_source) == {
        "x-agent-foundry-call-id": "call-2"
    }


@pytest.mark.asyncio
async def test_get_replaces_the_entry_and_cleans_up_the_old_session_and_toolbox_on_token_rotation():
    calls: list = []
    credential = _FakeCredential(["token-a", "token-b"])
    toolbox_sessions = [
        _FakeToolboxSession(calls, tools=["toolbox-tool-1"]),
        _FakeToolboxSession(calls, tools=["toolbox-tool-2"]),
    ]
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=toolbox_sessions)
    fixture = _make_manager(credential=credential, toolbox_runtime=toolbox_runtime, calls=calls)

    first = await fixture.manager.get("conv-1")
    first_toolbox = toolbox_sessions[0]
    second = await fixture.manager.get("conv-1")

    assert second is not first
    assert first.aborted is True
    assert first_toolbox.closed is True
    assert toolbox_runtime.connect_count == 2


# ── abort_turn(): aborts but keeps cache + toolbox ───────────────────────────


@pytest.mark.asyncio
async def test_abort_turn_aborts_the_cached_session_but_keeps_the_cache_and_toolbox():
    calls: list = []
    toolbox_session = _FakeToolboxSession(calls, tools=["toolbox-tool"])
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=[toolbox_session])
    fixture = _make_manager(toolbox_runtime=toolbox_runtime, calls=calls)

    session = await fixture.manager.get("conv-1")
    await fixture.manager.abort_turn("conv-1")

    assert session.aborted is True
    assert toolbox_session.closed is False

    calls.clear()
    same_session = await fixture.manager.get("conv-1")

    assert same_session is session
    assert calls == []  # cache untouched -> no reconnect / resume / create


@pytest.mark.asyncio
async def test_abort_turn_on_an_unknown_conversation_is_a_no_op():
    fixture = _make_manager()

    await fixture.manager.abort_turn("never-seen")  # must not raise


# ── reset(): drops cache, aborts, closes toolbox, removes the lock ──────────


@pytest.mark.asyncio
async def test_reset_drops_the_cache_aborts_the_session_and_closes_the_toolbox_session():
    calls: list = []
    toolbox_session = _FakeToolboxSession(calls, tools=["toolbox-tool"])
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=[toolbox_session])
    fixture = _make_manager(toolbox_runtime=toolbox_runtime, calls=calls)

    session = await fixture.manager.get("conv-1")
    await fixture.manager.reset("conv-1")

    assert session.aborted is True
    assert toolbox_session.closed is True

    calls.clear()
    await fixture.manager.get("conv-1")

    sid = fixture.manager.session_id_for("conv-1")
    assert ("resume_session", sid) in calls  # rebuilt fresh, not reused


@pytest.mark.asyncio
async def test_reset_removes_the_conversations_lock():
    fixture = _make_manager()
    lock_before = fixture.manager.lock_for("conv-1")
    await fixture.manager.get("conv-1")

    await fixture.manager.reset("conv-1")

    lock_after = fixture.manager.lock_for("conv-1")
    assert lock_after is not lock_before


@pytest.mark.asyncio
async def test_reset_propagates_a_session_abort_failure_instead_of_silently_swallowing_it():
    calls: list = []

    class _BoomClient(_FakeClient):
        async def resume_session(self, session_id, **opts):
            self._calls.append(("resume_session", session_id))
            session = _FakeSession(session_id, opts)

            async def _boom():
                raise RuntimeError("abort boom")

            session.abort = _boom
            return session

    fixture = _make_manager(client=_BoomClient(calls), calls=calls)
    await fixture.manager.get("conv-1")

    with pytest.raises(RuntimeError, match="abort boom"):
        await fixture.manager.reset("conv-1")


# ── close(): resets every conversation, stops the client if it can ─────────


@pytest.mark.asyncio
async def test_close_resets_every_conversation_and_stops_a_client_that_exposes_stop():
    calls: list = []
    toolbox_sessions = [
        _FakeToolboxSession(calls, tools=["toolbox-tool-1"]),
        _FakeToolboxSession(calls, tools=["toolbox-tool-2"]),
    ]
    toolbox_runtime = _FakeToolboxRuntime(calls, sessions=toolbox_sessions)
    fixture = _make_manager(toolbox_runtime=toolbox_runtime, calls=calls)

    session_a = await fixture.manager.get("conv-a")
    session_b = await fixture.manager.get("conv-b")

    await fixture.manager.close()

    assert session_a.aborted is True
    assert session_b.aborted is True
    assert all(session.closed for session in toolbox_sessions)
    assert fixture.client.stopped is True


@pytest.mark.asyncio
async def test_close_does_not_raise_when_the_client_has_no_stop_method():
    calls: list = []
    client = _FakeClientNoStop(calls)
    fixture = _make_manager(client=client, calls=calls)
    await fixture.manager.get("conv-1")

    await fixture.manager.close()  # must not raise despite no stop()

    assert not hasattr(client, "stop")
