"""Toolbox (MCP) connection lifecycle seam.

Wires an injected ``httpx.AsyncClient`` factory and an injected
``mcp_bridge.McpBridge``-shaped bridge factory together to establish an MCP
connection, perform the ``initialize``/``list_tools`` handshake, and convert
the resulting MCP tool catalog into real Copilot SDK tools guarded by the
Work IQ hard policy. Kept independent of any real Foundry request-context
plumbing beyond the thin ``current_platform_headers`` adapter so the
connect/close lifecycle itself can be unit tested with plain fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx
from azure.ai.agentserver.core import get_request_context

import toolbox_tools
import workiq_policy
from mcp_bridge import McpBridge

ClientFactory = Callable[[], httpx.AsyncClient]
BridgeFactory = Callable[..., Any]
PlatformHeadersFn = Callable[[], dict[str, str]]


class ToolboxUnavailableError(Exception):
    """Raised when connecting to the toolbox MCP endpoint fails.

    The original exception is preserved as ``__cause__``.
    """

    def __init__(self, phase: str, error: Exception) -> None:
        self.phase = phase
        self.cause_type = type(error).__name__
        self.code = getattr(error, "code", None)
        super().__init__(
            f"Work IQ toolbox {phase} failed ({self.cause_type}, code={self.code})"
        )


def default_client_factory() -> httpx.AsyncClient:
    """Create an MCP client with timeouts suitable for Graph-backed tools."""
    return httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=10.0, pool=10.0))


def current_platform_headers() -> dict[str, str]:
    """Return a fresh copy of the current request's platform headers.

    Safe to call with no AgentServer request context bound (e.g. outside a
    request, or local development): returns ``{}`` in that case.
    """
    return dict(get_request_context().platform_headers())


@dataclass
class ToolboxSession:
    """A live MCP connection plus the Copilot tools built from its catalog."""

    bridge: Any
    tools: list[Any]

    async def close(self) -> None:
        """Close the underlying bridge."""
        await self.bridge.close()


class ToolboxRuntime:
    """Establishes (and can re-establish) a Toolbox MCP connection."""

    def __init__(
        self,
        endpoint: str,
        credential: Any,
        *,
        client_factory: ClientFactory = default_client_factory,
        bridge_factory: BridgeFactory = McpBridge,
        platform_headers: PlatformHeadersFn = current_platform_headers,
        authorize: Callable[..., Any] = workiq_policy.evaluate,
        build_tools: Callable[..., Any] = toolbox_tools.build_copilot_tools,
    ) -> None:
        self._endpoint = endpoint
        self._credential = credential
        self._client_factory = client_factory
        self._bridge_factory = bridge_factory
        self._platform_headers = platform_headers
        self._authorize = authorize
        self._build_tools = build_tools

    async def connect(
        self,
        *,
        platform_headers: PlatformHeadersFn | None = None,
    ) -> ToolboxSession | None:
        """Connect to the toolbox MCP endpoint, or return ``None`` if unset.

        Raises ``ToolboxUnavailableError`` (with the original exception
        preserved as ``__cause__``) if the handshake or tool-building fails,
        after closing the bridge created for the attempt.
        """
        if not self._endpoint.strip():
            return None

        client = self._client_factory()
        bridge = self._bridge_factory(
            self._endpoint,
            self._credential,
            client,
            platform_headers or self._platform_headers,
            owns_client=True,
        )

        phase = "initialize"
        try:
            await bridge.initialize()
            phase = "list_tools"
            mcp_tools = await bridge.list_tools()
            phase = "build_tools"
            tools = self._build_tools(bridge, mcp_tools, self._authorize)
        except Exception as error:
            await bridge.close()
            raise ToolboxUnavailableError(phase, error) from error

        return ToolboxSession(bridge, tools)
