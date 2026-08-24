"""Tests for the Toolbox (MCP) connection lifecycle seam (toolbox_runtime).

Expected (not yet implemented) production module: ``toolbox_runtime``

  - ``ToolboxRuntime(endpoint, credential, *, client_factory=httpx.AsyncClient,
    bridge_factory=mcp_bridge.McpBridge, platform_headers=current_platform_headers,
    authorize=workiq_policy.evaluate, build_tools=toolbox_tools.build_copilot_tools)``

  - ``await connect() -> ToolboxSession | None``
      * a blank ``endpoint`` (empty / whitespace-only) short-circuits: returns
        ``None`` and creates *nothing* (no client, no bridge).
      * otherwise: creates exactly one client via ``client_factory()``, one
        bridge via
        ``bridge_factory(endpoint, credential, client, platform_headers, owns_client=True)``,
        ``await``s ``bridge.initialize()`` then ``await``s ``bridge.list_tools()``,
        builds real Copilot tools via
        ``build_tools(bridge, mcp_tools, authorize)`` and returns
        ``ToolboxSession(bridge, tools)``.
      * if ``initialize`` / ``list_tools`` / ``build_tools`` raises, the bridge
        is closed and a ``ToolboxUnavailableError`` is raised with the
        original exception preserved as ``__cause__``. No success-shaped
        fallback is produced by this layer.

  - ``ToolboxSession``
      * ``.tools`` is exactly the list returned by ``build_tools``.
      * ``await close()`` closes the session's bridge (and nothing else --
        closing the owned client is the bridge's own responsibility).

  - ``current_platform_headers() -> dict[str, str]`` returns
    ``dict(get_request_context().platform_headers())`` while an AgentServer
    request context is bound, and ``{}`` when none is bound. Uses the real
    ``azure.ai.agentserver.core`` request-context contextvar (local, no
    network) rather than a mock.
"""

from __future__ import annotations

import pytest
from azure.ai.agentserver.core import (
    FoundryAgentRequestContext,
    reset_request_context,
    set_request_context,
)

import httpx
import workiq_policy
from mcp_bridge import McpBridge  # type: ignore[import-not-found]

from toolbox_runtime import (  # type: ignore[import-not-found]
    ToolboxRuntime,
    ToolboxSession,
    ToolboxUnavailableError,
    current_platform_headers,
)

_ENDPOINT = "https://mcp.example.test/mcp"


class _Credential:
    """Opaque sentinel: ToolboxRuntime must forward it, never call it itself."""


class _FakeClient:
    """Complete async double standing in for ``httpx.AsyncClient``."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeBridge:
    """Complete async double for ``mcp_bridge.McpBridge``'s connect-time seam."""

    def __init__(self, calls, *, tools=None, init_error=None, list_error=None):
        self._calls = calls
        self._tools = tools if tools is not None else [{"name": "toolboxA___echo"}]
        self._init_error = init_error
        self._list_error = list_error
        self.closed = False

    async def initialize(self) -> None:
        self._calls.append("initialize")
        if self._init_error is not None:
            raise self._init_error

    async def list_tools(self):
        self._calls.append("list_tools")
        if self._list_error is not None:
            raise self._list_error
        return self._tools

    async def close(self) -> None:
        self._calls.append("close")
        self.closed = True


def _factories(bridge, calls, *, client=None, bridge_factory_args=None):
    client = client if client is not None else _FakeClient()

    def client_factory():
        calls.append("client_factory")
        return client

    def bridge_factory(endpoint, credential, client_arg, platform_headers, *, owns_client):
        calls.append("bridge_factory")
        if bridge_factory_args is not None:
            bridge_factory_args.update(
                endpoint=endpoint,
                credential=credential,
                client=client_arg,
                platform_headers=platform_headers,
                owns_client=owns_client,
            )
        return bridge

    return client, client_factory, bridge_factory


def _build_tools_recording(calls, result):
    build_tools_args: dict = {}

    def build_tools(bridge_arg, mcp_tools, authorize):
        calls.append("build_tools")
        build_tools_args.update(bridge=bridge_arg, mcp_tools=mcp_tools, authorize=authorize)
        return result

    return build_tools, build_tools_args


def _allow(_name, _arguments):
    return workiq_policy.PolicyResult(workiq_policy.PolicyDecision.ALLOW, "always allowed in this test")


# ── blank endpoint short-circuit ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_endpoint", ["", "   "])
async def test_connect_returns_none_and_creates_nothing_for_a_blank_endpoint(blank_endpoint):
    calls: list[str] = []
    bridge = _FakeBridge(calls)
    _, client_factory, bridge_factory = _factories(bridge, calls)
    build_tools, _ = _build_tools_recording(calls, ["unused"])

    runtime = ToolboxRuntime(
        blank_endpoint,
        _Credential(),
        client_factory=client_factory,
        bridge_factory=bridge_factory,
        platform_headers=lambda: {},
        authorize=_allow,
        build_tools=build_tools,
    )

    session = await runtime.connect()

    assert session is None
    assert calls == []  # nothing created: no client, no bridge, no tools built


# ── happy path: call order, wiring, and returned session ────────────────────


@pytest.mark.asyncio
async def test_connect_creates_one_client_and_one_bridge_in_the_expected_order():
    calls: list[str] = []
    mcp_tools = [{"name": "toolboxA___echo"}]
    bridge = _FakeBridge(calls, tools=mcp_tools)
    bridge_factory_args: dict = {}
    client, client_factory, bridge_factory = _factories(
        bridge, calls, bridge_factory_args=bridge_factory_args
    )
    built_tools = ["real-copilot-tool-sentinel"]
    build_tools, build_tools_args = _build_tools_recording(calls, built_tools)
    platform_headers_fn = lambda: {"x-agent-foundry-call-id": "call-1"}
    credential = _Credential()

    runtime = ToolboxRuntime(
        _ENDPOINT,
        credential,
        client_factory=client_factory,
        bridge_factory=bridge_factory,
        platform_headers=platform_headers_fn,
        authorize=_allow,
        build_tools=build_tools,
    )
    session = await runtime.connect()

    assert calls == ["client_factory", "bridge_factory", "initialize", "list_tools", "build_tools"]
    assert bridge_factory_args == {
        "endpoint": _ENDPOINT,
        "credential": credential,
        "client": client,
        "platform_headers": platform_headers_fn,
        "owns_client": True,
    }
    assert build_tools_args == {"bridge": bridge, "mcp_tools": mcp_tools, "authorize": _allow}
    assert isinstance(session, ToolboxSession)
    assert session.tools is built_tools


@pytest.mark.asyncio
async def test_connect_can_override_platform_headers_for_a_cached_conversation_session():
    calls: list[str] = []
    bridge = _FakeBridge(calls)
    bridge_factory_args: dict = {}
    _, client_factory, bridge_factory = _factories(
        bridge, calls, bridge_factory_args=bridge_factory_args
    )
    build_tools, _ = _build_tools_recording(calls, [])
    runtime_headers = lambda: {"x-agent-foundry-call-id": "ambient"}
    turn_headers = lambda: {"x-agent-foundry-call-id": "captured"}
    runtime = ToolboxRuntime(
        _ENDPOINT,
        _Credential(),
        client_factory=client_factory,
        bridge_factory=bridge_factory,
        platform_headers=runtime_headers,
        authorize=_allow,
        build_tools=build_tools,
    )

    await runtime.connect(platform_headers=turn_headers)

    assert bridge_factory_args["platform_headers"] is turn_headers


@pytest.mark.asyncio
async def test_session_close_closes_its_bridge_and_nothing_else_directly():
    calls: list[str] = []
    bridge = _FakeBridge(calls)
    client, client_factory, bridge_factory = _factories(bridge, calls)
    build_tools, _ = _build_tools_recording(calls, [])

    runtime = ToolboxRuntime(
        _ENDPOINT,
        _Credential(),
        client_factory=client_factory,
        bridge_factory=bridge_factory,
        platform_headers=lambda: {},
        authorize=_allow,
        build_tools=build_tools,
    )
    session = await runtime.connect()
    calls.clear()

    await session.close()

    assert calls == ["close"]  # delegates solely to bridge.close()
    assert bridge.closed is True
    assert client.closed is False  # closing the owned client is the bridge's job, not the session's


# ── failure cleanup + typed error with preserved cause ──────────────────────


@pytest.mark.asyncio
async def test_connect_closes_the_bridge_and_raises_toolbox_unavailable_error_when_initialize_fails():
    calls: list[str] = []
    original_error = RuntimeError("initialize boom")
    bridge = _FakeBridge(calls, init_error=original_error)
    _, client_factory, bridge_factory = _factories(bridge, calls)
    build_tools, _ = _build_tools_recording(calls, [])

    runtime = ToolboxRuntime(
        _ENDPOINT,
        _Credential(),
        client_factory=client_factory,
        bridge_factory=bridge_factory,
        platform_headers=lambda: {},
        authorize=_allow,
        build_tools=build_tools,
    )

    with pytest.raises(ToolboxUnavailableError) as exc_info:
        await runtime.connect()

    assert exc_info.value.__cause__ is original_error
    assert exc_info.value.phase == "initialize"
    assert bridge.closed is True
    assert "build_tools" not in calls  # never reached


@pytest.mark.asyncio
async def test_connect_closes_the_bridge_and_raises_toolbox_unavailable_error_when_list_tools_fails():
    calls: list[str] = []
    original_error = RuntimeError("list_tools boom")
    bridge = _FakeBridge(calls, list_error=original_error)
    _, client_factory, bridge_factory = _factories(bridge, calls)
    build_tools, _ = _build_tools_recording(calls, [])

    runtime = ToolboxRuntime(
        _ENDPOINT,
        _Credential(),
        client_factory=client_factory,
        bridge_factory=bridge_factory,
        platform_headers=lambda: {},
        authorize=_allow,
        build_tools=build_tools,
    )

    with pytest.raises(ToolboxUnavailableError) as exc_info:
        await runtime.connect()

    assert exc_info.value.__cause__ is original_error
    assert exc_info.value.phase == "list_tools"
    assert bridge.closed is True
    assert "build_tools" not in calls  # never reached


@pytest.mark.asyncio
async def test_connect_closes_the_bridge_and_raises_toolbox_unavailable_error_when_build_tools_fails():
    calls: list[str] = []
    bridge = _FakeBridge(calls)
    _, client_factory, bridge_factory = _factories(bridge, calls)
    original_error = ValueError("build_tools boom")

    def failing_build_tools(_bridge, _mcp_tools, _authorize):
        calls.append("build_tools")
        raise original_error

    runtime = ToolboxRuntime(
        _ENDPOINT,
        _Credential(),
        client_factory=client_factory,
        bridge_factory=bridge_factory,
        platform_headers=lambda: {},
        authorize=_allow,
        build_tools=failing_build_tools,
    )

    with pytest.raises(ToolboxUnavailableError) as exc_info:
        await runtime.connect()

    assert exc_info.value.__cause__ is original_error
    assert exc_info.value.phase == "build_tools"
    assert bridge.closed is True


# ── production defaults are wired without ever being invoked ───────────────


def test_default_collaborators_are_the_production_ones_without_connecting():
    import toolbox_tools
    from toolbox_runtime import default_client_factory

    runtime = ToolboxRuntime(_ENDPOINT, _Credential())

    assert runtime._client_factory is default_client_factory
    assert runtime._bridge_factory is McpBridge
    assert runtime._platform_headers is current_platform_headers
    assert runtime._authorize is workiq_policy.evaluate
    assert runtime._build_tools is toolbox_tools.build_copilot_tools


@pytest.mark.asyncio
async def test_default_client_factory_uses_graph_appropriate_timeouts():
    from toolbox_runtime import default_client_factory

    client = default_client_factory()
    try:
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 75.0
        assert client.timeout.write == 75.0
        assert client.timeout.pool == 10.0
    finally:
        await client.aclose()


# ── current_platform_headers ────────────────────────────────────────────────


def test_current_platform_headers_returns_an_empty_dict_when_no_request_context_is_bound():
    assert current_platform_headers() == {}


def test_current_platform_headers_returns_the_bound_context_platform_headers_during_a_request():
    ctx = FoundryAgentRequestContext(call_id="call-123")
    expected = dict(ctx.platform_headers())
    token = set_request_context(ctx)
    try:
        assert current_platform_headers() == expected
    finally:
        reset_request_context(token)


def test_current_platform_headers_returns_a_fresh_copy_not_a_live_view():
    ctx = FoundryAgentRequestContext(call_id="call-456")
    token = set_request_context(ctx)
    try:
        result = current_platform_headers()
        result["mutated"] = "yes"

        assert "mutated" not in current_platform_headers()
    finally:
        reset_request_context(token)
