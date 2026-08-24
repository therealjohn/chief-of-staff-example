"""MCP JSON-RPC bridge over an injected ``httpx.AsyncClient``.

Network-free at the unit-test level (the caller supplies the client, e.g.
wired to ``httpx.MockTransport``); this module has no Azure / M365 imports
and makes no assumptions about the transport beyond the ``httpx`` API.

A fresh bearer token and the current platform headers are evaluated on
*every* HTTP request -- including the ``initialize`` call itself -- since
tokens can expire and platform headers (e.g. a per-call id) can vary between
calls.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable

import httpx

_DEFAULT_SCOPE = "https://ai.azure.com/.default"
_PROTOCOL_VERSION = "2025-06-18"


class McpProtocolError(Exception):
    """Raised when an MCP JSON-RPC response carries an ``error`` object."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code


class McpBridge:
    """Thin MCP client speaking JSON-RPC over HTTP via an injected client."""

    def __init__(
        self,
        endpoint: str,
        credential: Any,
        client: httpx.AsyncClient,
        platform_headers: Callable[[], dict[str, str]],
        *,
        owns_client: bool = False,
        scope: str = _DEFAULT_SCOPE,
    ) -> None:
        self._endpoint = endpoint
        self._credential = credential
        self._client = client
        self._platform_headers = platform_headers
        self._owns_client = owns_client
        self._scope = scope
        self._ids = itertools.count(1)
        self.session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        token = self._credential.get_token(self._scope).token
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "foundry-features": "Toolboxes=V1Preview",
        }
        headers.update(self._platform_headers())
        if self.session_id is not None:
            headers["mcp-session-id"] = self.session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        response = await self._client.post(self._endpoint, json=payload, headers=self._headers())
        response.raise_for_status()
        return response

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            payload["params"] = params
        response = await self._post(payload)
        body = response.json()
        if "error" in body:
            error = body["error"]
            raise McpProtocolError(error.get("code"), error.get("message", ""))
        return body["result"]

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._post(payload)

    async def initialize(self) -> None:
        """Perform the MCP handshake: ``initialize`` then ``notifications/initialized``."""
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-bridge", "version": "0.1.0"},
            },
        }
        response = await self._post(payload)
        body = response.json()
        if "error" in body:
            error = body["error"]
            raise McpProtocolError(error.get("code"), error.get("message", ""))
        self.session_id = response.headers.get("mcp-session-id")
        await self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._send_request("tools/list")
        return result["tools"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._send_request("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        """Close the underlying client only when this bridge owns it."""
        if self._owns_client:
            await self._client.aclose()
