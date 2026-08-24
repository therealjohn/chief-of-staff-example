"""Tests for the MCP -> Copilot SDK tool bridge (toolbox_tools).

Expected (not yet implemented) production module: ``toolbox_tools``

  - ``sanitize_tool_name(name: str) -> str`` -- strips an MCP server prefix:
    everything up to and including the *last* ``___`` if the name contains
    one; otherwise, for a dotted server prefix, everything up to and
    including the *last* ``.``. Any character outside ``[A-Za-z0-9_]`` in
    what remains is replaced with ``_``. The result is guaranteed to be a
    legal, non-empty Python-identifier-shaped string -- including a
    deterministic fix-up when the remaining name would start with a digit,
    or would otherwise be empty.

  - ``build_copilot_tools(bridge, mcp_tools, authorize) -> list[copilot.tools.Tool]``
    -- converts MCP tool descriptors (dicts with ``name``/``description``/
    ``inputSchema``) into real ``copilot.tools.Tool`` objects.

    * The tool's ``name`` is ``sanitize_tool_name(mcp_tool["name"])``;
      colliding sanitized names are disambiguated in input order with
      stable ``_2``, ``_3``, ... suffixes.
    * ``description`` / ``parameters`` are copied verbatim from
      ``description`` / ``inputSchema``.
    * Each built ``Tool``'s handler is a ``ToolInvocation -> ToolResult``
      coroutine that:
        1. reads ``invocation.arguments``,
        2. calls ``authorize(original_mcp_name, arguments)`` (a
           ``workiq_policy.evaluate``-shaped sync callable returning a
           ``workiq_policy.PolicyResult``) *before* ever touching the
           bridge -- the *original*, unsanitized MCP name is used both here
           and for the bridge call,
        3. on ``PolicyDecision.DENY``, returns
           ``ToolResult(result_type="denied")`` and never calls the bridge,
        4. on ``PolicyDecision.ALLOW``, calls
           ``await bridge.call_tool(original_mcp_name, arguments)`` and
           returns ``copilot.tools.convert_mcp_call_tool_result(result)``,
        5. if the bridge raises ``mcp_bridge.McpProtocolError``, returns a
           failure-shaped ``ToolResult`` (``result_type="failure"``) --
           never a success-shaped result.
"""

from __future__ import annotations

import re

import httpx
import pytest

from copilot import Tool, ToolInvocation, convert_mcp_call_tool_result
from mcp_bridge import McpProtocolError  # type: ignore[import-not-found]
from workiq_policy import PolicyDecision, PolicyResult  # type: ignore[import-not-found]

from toolbox_tools import (  # type: ignore[import-not-found]
    build_copilot_tools,
    sanitize_tool_name,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── sanitize_tool_name ──────────────────────────────────────────────────────


def test_sanitize_tool_name_strips_the_mcp_server_prefix_before_the_last_triple_underscore():
    assert sanitize_tool_name("toolboxA___mail_search") == "mail_search"


def test_sanitize_tool_name_strips_before_the_last_triple_underscore_with_multiple_occurrences():
    assert sanitize_tool_name("srv___sub___tool") == "tool"


def test_sanitize_tool_name_strips_a_dotted_server_prefix_when_there_is_no_triple_underscore():
    assert sanitize_tool_name("myServer.echo") == "echo"


def test_sanitize_tool_name_strips_before_the_last_dot_of_a_multi_segment_dotted_prefix():
    assert sanitize_tool_name("org.myServer.echo") == "echo"


def test_sanitize_tool_name_prefers_the_triple_underscore_split_over_a_dotted_prefix():
    assert sanitize_tool_name("toolboxes.echo___tool_name") == "tool_name"


def test_sanitize_tool_name_replaces_invalid_function_name_characters_with_underscore():
    assert sanitize_tool_name("weird tool:name-here!") == "weird_tool_name_here_"


def test_sanitize_tool_name_is_deterministic_for_a_name_beginning_with_a_digit():
    first = sanitize_tool_name("123_thing")
    second = sanitize_tool_name("123_thing")

    assert first == second
    assert _IDENTIFIER_RE.match(first)


@pytest.mark.parametrize("name", ["0start", "9lives", "42"])
def test_sanitize_tool_name_never_returns_a_name_beginning_with_a_digit(name):
    result = sanitize_tool_name(name)

    assert result
    assert not result[0].isdigit()
    assert _IDENTIFIER_RE.match(result)


def test_sanitize_tool_name_returns_a_legal_non_empty_identifier_for_a_prefix_only_name():
    result = sanitize_tool_name("toolboxA___")

    assert result
    assert _IDENTIFIER_RE.match(result)


def test_sanitize_tool_name_returns_a_legal_non_empty_identifier_for_an_empty_name():
    result = sanitize_tool_name("")

    assert result
    assert _IDENTIFIER_RE.match(result)


# ── build_copilot_tools ─────────────────────────────────────────────────────


def _mcp_tool(name: str, *, description: str = "", schema: dict | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else {"type": "object"},
    }


class _FakeBridge:
    """Complete async double for the ``mcp_bridge.McpBridge.call_tool`` seam."""

    def __init__(self, *, result: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._result = result
        self._error = error

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if self._error is not None:
            raise self._error
        return self._result


def _allow(_name, _arguments):
    return PolicyResult(PolicyDecision.ALLOW, "always allowed in this test")


def _deny(_name, _arguments):
    return PolicyResult(PolicyDecision.DENY, "always denied in this test")


@pytest.mark.asyncio
async def test_build_copilot_tools_returns_a_real_copilot_tool_per_mcp_descriptor():
    bridge = _FakeBridge()
    mcp_tools = [
        _mcp_tool(
            "toolboxA___mail_search",
            description="Search mail",
            schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
    ]

    tools = build_copilot_tools(bridge, mcp_tools, _allow)

    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, Tool)
    assert tool.name == "mail_search"
    assert tool.description == "Search mail"
    assert tool.parameters == {"type": "object", "properties": {"q": {"type": "string"}}}


@pytest.mark.asyncio
async def test_build_copilot_tools_deduplicates_colliding_sanitized_names_with_stable_numeric_suffixes():
    bridge = _FakeBridge()
    mcp_tools = [
        _mcp_tool("toolboxA___frobnicate", description="A"),
        _mcp_tool("toolboxB___frobnicate", description="B"),
    ]

    tools = build_copilot_tools(bridge, mcp_tools, _allow)

    assert [t.name for t in tools] == ["frobnicate", "frobnicate_2"]
    assert [t.description for t in tools] == ["A", "B"]


@pytest.mark.asyncio
async def test_build_copilot_tools_handler_preserves_the_original_mcp_name_for_the_bridge_call():
    call_result = {"content": [{"type": "text", "text": "3 emails found"}], "isError": False}
    bridge = _FakeBridge(result=call_result)
    mcp_tools = [_mcp_tool("toolboxA___mail_search")]
    tool = build_copilot_tools(bridge, mcp_tools, _allow)[0]

    result = await tool.handler(ToolInvocation(arguments={"query": "invoice"}))

    assert bridge.calls == [("toolboxA___mail_search", {"query": "invoice"})]
    assert result == convert_mcp_call_tool_result(call_result)


@pytest.mark.asyncio
async def test_build_copilot_tools_handler_returns_a_success_result_on_an_allowed_call():
    call_result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
    bridge = _FakeBridge(result=call_result)
    mcp_tools = [_mcp_tool("toolboxA___mail_search")]
    tool = build_copilot_tools(bridge, mcp_tools, _allow)[0]

    result = await tool.handler(ToolInvocation(arguments={}))

    assert result.result_type == "success"
    assert result.text_result_for_llm == "ok"


@pytest.mark.asyncio
async def test_build_copilot_tools_handler_calls_authorize_with_the_original_mcp_name_and_arguments():
    bridge = _FakeBridge(result={"content": [], "isError": False})
    seen: list[tuple[str, dict]] = []

    def authorize(name, arguments):
        seen.append((name, arguments))
        return PolicyResult(PolicyDecision.ALLOW, "ok")

    mcp_tools = [_mcp_tool("toolboxA___mail_search")]
    tool = build_copilot_tools(bridge, mcp_tools, authorize)[0]

    await tool.handler(ToolInvocation(arguments={"query": "invoice"}))

    assert seen == [("toolboxA___mail_search", {"query": "invoice"})]


@pytest.mark.asyncio
async def test_build_copilot_tools_handler_denies_without_ever_calling_the_bridge():
    bridge = _FakeBridge(result={"content": [], "isError": False})
    mcp_tools = [_mcp_tool("toolboxA___mail_send")]
    tool = build_copilot_tools(bridge, mcp_tools, _deny)[0]

    result = await tool.handler(ToolInvocation(arguments={}))

    assert result.result_type == "denied"
    assert result.text_result_for_llm == "Tool call denied: always denied in this test"
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_build_copilot_tools_handler_normalizes_missing_arguments_to_an_empty_mapping():
    bridge = _FakeBridge(result={"content": [], "isError": False})
    seen: list[dict] = []

    def authorize(_name, arguments):
        seen.append(arguments)
        return PolicyResult(PolicyDecision.ALLOW, "ok")

    tool = build_copilot_tools(bridge, [_mcp_tool("toolboxA___mail_search")], authorize)[0]

    await tool.handler(ToolInvocation(arguments=None))

    assert seen == [{}]
    assert bridge.calls == [("toolboxA___mail_search", {})]


@pytest.mark.asyncio
async def test_build_copilot_tools_handler_returns_a_failure_result_never_a_success_shape_on_protocol_error():
    bridge = _FakeBridge(error=McpProtocolError(-32000, "downstream boom"))
    mcp_tools = [_mcp_tool("toolboxA___mail_search")]
    tool = build_copilot_tools(bridge, mcp_tools, _allow)[0]

    result = await tool.handler(ToolInvocation(arguments={}))

    assert result.result_type == "failure"
    assert result.result_type != "success"


@pytest.mark.asyncio
async def test_build_copilot_tools_handler_returns_failure_on_http_timeout():
    bridge = _FakeBridge(error=httpx.ReadTimeout("tool call timed out"))
    tool = build_copilot_tools(
        bridge,
        [_mcp_tool("toolboxA___mail_search")],
        _allow,
    )[0]

    result = await tool.handler(ToolInvocation(arguments={}))

    assert result.result_type == "failure"
    assert "timed out" in result.text_result_for_llm.lower()
