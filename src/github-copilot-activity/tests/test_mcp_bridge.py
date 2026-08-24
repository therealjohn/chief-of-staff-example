"""Tests for the MCP JSON-RPC bridge over httpx (item 3). Network-free: every
request is served by ``httpx.MockTransport``; no Azure/M365 imports.

Expected (not yet implemented) production module: ``mcp_bridge``
  - ``McpBridge(endpoint, credential, client, platform_headers, *, owns_client=False)``
    where ``credential.get_token(scope).token`` is sync, ``client`` is a caller
    supplied ``httpx.AsyncClient``, and ``platform_headers()`` is a sync
    callable returning the current platform header dict.
  - ``await initialize()`` -- MCP ``initialize`` then ``notifications/initialized``;
    captures the ``mcp-session-id`` response header on ``self.session_id``.
  - ``await list_tools()`` -- returns the ``tools`` array from ``tools/list``.
  - ``await call_tool(name, arguments)`` -- returns the JSON-RPC ``result``;
    raises ``McpProtocolError`` (with ``.code``) when the response has ``error``.
  - ``await close()`` -- closes ``client`` only when ``owns_client`` is True.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mcp_bridge import McpBridge, McpProtocolError  # type: ignore[import-not-found]

_ENDPOINT = "https://mcp.example.test/mcp"


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    """Sync credential mirroring azure-identity's get_token(scope).token."""

    def __init__(self, token: str = "tok-0") -> None:
        self.token = token
        self.calls: list[str] = []

    def get_token(self, scope: str) -> _FakeToken:
        self.calls.append(scope)
        return _FakeToken(self.token)


def _capture(handler):
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return requests, wrapped


def _dispatcher(*, session_id="session-abc", tools=None, call_error=None):
    tools = tools if tools is not None else [{"name": "echo", "description": "Echo back", "inputSchema": {"type": "object"}}]

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        req_id = payload.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
                    },
                },
                headers={"mcp-session-id": session_id},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})
        if method == "tools/call":
            if call_error is not None:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "error": call_error})
            args = payload["params"]["arguments"]
            result = {"content": [{"type": "text", "text": args.get("text", "")}], "isError": False}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": result})
        raise AssertionError(f"unexpected MCP method: {method}")

    return handle


def _bridge(handler, *, credential=None, platform_headers=None, owns_client=False):
    requests, wrapped = _capture(handler)
    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    credential = credential or _FakeCredential()
    platform_headers = platform_headers or (lambda: {"x-agent-foundry-call-id": "call-1"})
    bridge = McpBridge(_ENDPOINT, credential, client, platform_headers, owns_client=owns_client)
    return bridge, requests, client, credential


@pytest.mark.asyncio
async def test_initialize_sends_initialize_then_notifications_initialized_and_captures_session_id():
    bridge, requests, client, _ = _bridge(_dispatcher(session_id="session-xyz"))

    await bridge.initialize()

    assert [json.loads(r.content)["method"] for r in requests] == ["initialize", "notifications/initialized"]
    assert bridge.session_id == "session-xyz"
    await client.aclose()


@pytest.mark.asyncio
async def test_list_tools_returns_the_tools_array():
    tools = [{"name": "echo", "description": "Echo back", "inputSchema": {"type": "object"}}]
    bridge, _, client, _ = _bridge(_dispatcher(tools=tools))
    await bridge.initialize()

    result = await bridge.list_tools()

    assert result == tools
    await client.aclose()


@pytest.mark.asyncio
async def test_call_tool_returns_the_mcp_result_object():
    bridge, requests, client, _ = _bridge(_dispatcher())
    await bridge.initialize()

    result = await bridge.call_tool("echo", {"text": "hi"})

    assert result["content"][0]["text"] == "hi"
    call_request = json.loads(requests[-1].content)
    assert call_request["params"] == {"name": "echo", "arguments": {"text": "hi"}}
    await client.aclose()


@pytest.mark.asyncio
async def test_call_tool_raises_mcp_protocol_error_on_jsonrpc_error():
    bridge, _, client, _ = _bridge(_dispatcher(call_error={"code": -32602, "message": "Invalid params"}))
    await bridge.initialize()

    with pytest.raises(McpProtocolError) as exc_info:
        await bridge.call_tool("echo", {})

    assert exc_info.value.code == -32602
    assert "Invalid params" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_every_request_gets_a_fresh_bearer_token_and_the_standard_platform_headers():
    call_ids = iter(["call-1", "call-2", "call-3", "call-4"])
    seen: list[str] = []

    def platform_headers():
        current = next(call_ids)
        seen.append(current)
        return {"x-agent-foundry-call-id": current}

    credential = _FakeCredential("tok-abc")
    bridge, requests, client, credential = _bridge(
        _dispatcher(), credential=credential, platform_headers=platform_headers
    )

    await bridge.initialize()  # requests[0]=initialize, requests[1]=notifications/initialized
    await bridge.list_tools()  # requests[2]
    await bridge.call_tool("echo", {"text": "hi"})  # requests[3]

    assert len(requests) == 4
    assert len(credential.calls) == 4  # a fresh token is obtained for every request

    for i, request in enumerate(requests):
        assert request.headers["authorization"] == "Bearer tok-abc"
        assert request.headers["foundry-features"] == "Toolboxes=V1Preview"
        assert request.headers["x-agent-foundry-call-id"] == seen[i]

    assert "mcp-session-id" not in requests[0].headers  # not yet known for the initialize call itself
    for request in requests[1:]:
        assert request.headers["mcp-session-id"] == bridge.session_id
    await client.aclose()


@pytest.mark.asyncio
async def test_close_leaves_an_injected_client_open_by_default():
    bridge, _, client, _ = _bridge(_dispatcher())

    await bridge.close()

    assert client.is_closed is False
    await client.aclose()


@pytest.mark.asyncio
async def test_close_closes_the_client_when_the_bridge_owns_it():
    bridge, _, client, _ = _bridge(_dispatcher(), owns_client=True)

    await bridge.close()

    assert client.is_closed is True
