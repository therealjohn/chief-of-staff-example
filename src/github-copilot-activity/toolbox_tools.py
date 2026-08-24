"""Bridge from MCP tool descriptors to real ``copilot.tools.Tool`` objects.

Converts the tool catalog exposed by an :class:`mcp_bridge.McpBridge` into
Copilot SDK tools: each built tool's handler enforces the Work IQ hard policy
(``workiq_policy.evaluate``-shaped ``authorize`` callable) *before* ever
touching the bridge, using the original (unsanitized) MCP tool name for both
the authorization check and the bridge call itself. Only the surfaced tool
``name`` is sanitized, to satisfy the Copilot SDK's function-name shape.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import httpx
from copilot import Tool, ToolInvocation, ToolResult, convert_mcp_call_tool_result
from mcp_bridge import McpProtocolError
from workiq_policy import PolicyDecision

_TRIPLE_UNDERSCORE = "___"
_INVALID_CHAR_RE = re.compile(r"[^A-Za-z0-9_]")
_EMPTY_NAME_FALLBACK = "tool"

AuthorizeFn = Callable[[str, dict[str, Any]], Any]


def sanitize_tool_name(name: str) -> str:
    """Strip an MCP server prefix from *name* and return a legal identifier.

    Everything up to and including the *last* ``___`` is stripped when present;
    otherwise, for a dotted server prefix, everything up to and including the
    *last* ``.`` is stripped. Any remaining character outside
    ``[A-Za-z0-9_]`` is replaced with ``_``. The result is guaranteed to be a
    non-empty, legal Python-identifier-shaped string.
    """
    if _TRIPLE_UNDERSCORE in name:
        remainder = name.rsplit(_TRIPLE_UNDERSCORE, 1)[1]
    elif "." in name:
        remainder = name.rsplit(".", 1)[1]
    else:
        remainder = name

    remainder = _INVALID_CHAR_RE.sub("_", remainder)

    if not remainder:
        remainder = _EMPTY_NAME_FALLBACK
    if remainder[0].isdigit():
        remainder = f"_{remainder}"

    return remainder


def _make_handler(bridge: Any, original_name: str, authorize: AuthorizeFn):
    async def handler(invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments if isinstance(invocation.arguments, dict) else {}
        policy_result = authorize(original_name, arguments)
        if policy_result.decision is PolicyDecision.DENY:
            return ToolResult(
                text_result_for_llm=f"Tool call denied: {policy_result.reason}",
                result_type="denied",
                error=policy_result.reason,
            )

        try:
            result = await bridge.call_tool(original_name, arguments)
        except httpx.TimeoutException as exc:
            return ToolResult(
                text_result_for_llm="Tool call failed: the Work IQ service timed out.",
                result_type="failure",
                error=str(exc),
            )
        except (McpProtocolError, httpx.HTTPError) as exc:
            return ToolResult(
                text_result_for_llm="Tool call failed: the Work IQ service request failed.",
                result_type="failure",
                error=str(exc),
            )

        return convert_mcp_call_tool_result(result)

    return handler


def build_copilot_tools(
    bridge: Any,
    mcp_tools: list[dict[str, Any]],
    authorize: AuthorizeFn,
) -> list[Tool]:
    """Convert MCP tool descriptors into real ``copilot.tools.Tool`` objects."""
    tools: list[Tool] = []
    seen_counts: dict[str, int] = {}

    for mcp_tool in mcp_tools:
        original_name = mcp_tool["name"]
        base_name = sanitize_tool_name(original_name)
        count = seen_counts.get(base_name, 0) + 1
        seen_counts[base_name] = count
        name = base_name if count == 1 else f"{base_name}_{count}"

        tools.append(
            Tool(
                name=name,
                description=mcp_tool.get("description", ""),
                parameters=mcp_tool.get("inputSchema"),
                handler=_make_handler(bridge, original_name, authorize),
            )
        )

    return tools
