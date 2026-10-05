"""Model Context Protocol (MCP) client adapter supporting stdio and SSE transports with strict owner allowlists."""

import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from core.tools.schemas import ToolDefinition, ToolResult, ToolRisk

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPServerConfig(BaseModel):
    """Owner-approved configuration pinning a specific MCP server endpoint and risk profile."""

    id: str
    name: str
    description: str = ""
    transport: Literal["stdio", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    default_risk: ToolRisk = ToolRisk.EXTERNAL_WRITE
    confirmation_required: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0.0)


class MCPAllowlist:
    """Explicit owner-managed repository of authorized MCP server configurations."""

    def __init__(self, initial_servers: list[MCPServerConfig] | None = None) -> None:
        """Initialize the allowlist with optional predefined server configurations.

        Args:
            initial_servers: List of verified MCPServerConfig objects.
        """
        self._servers: dict[str, MCPServerConfig] = {}
        if initial_servers:
            for s in initial_servers:
                self.add_server(s)

    def add_server(self, config: MCPServerConfig) -> None:
        """Register or update an owner-approved MCP server configuration.

        Args:
            config: MCPServerConfig instance.
        """
        self._servers[config.id] = config

    def remove_server(self, server_id: str) -> bool:
        """Revoke authorization for an MCP server by ID.

        Args:
            server_id: Unique identifier of the server.

        Returns:
            True if the server existed and was removed; False otherwise.
        """
        return self._servers.pop(server_id, None) is not None

    def get_server(self, server_id: str) -> MCPServerConfig | None:
        """Retrieve an approved MCP server configuration by identifier.

        Args:
            server_id: Server identifier.

        Returns:
            MCPServerConfig if present and permitted; None otherwise.
        """
        return self._servers.get(server_id)

    def list_servers(self) -> list[MCPServerConfig]:
        """List all currently authorized MCP server configurations.

        Returns:
            List of MCPServerConfig instances.
        """
        return list(self._servers.values())


class MCPClientAdapter:
    """Client adapter connecting to owner-approved MCP servers via stdio or SSE transports."""

    def __init__(self, allowlist: MCPAllowlist | None = None) -> None:
        """Initialize the client adapter with an owner allowlist.

        Args:
            allowlist: Repository of permitted MCP server configurations.
        """
        self.allowlist = allowlist or MCPAllowlist()
        self._stdio_processes: dict[str, asyncio.subprocess.Process] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, server_id: str) -> asyncio.Lock:
        """Retrieve or construct a concurrency lock for a specific server instance.

        Args:
            server_id: Server identifier.

        Returns:
            Dedicated asyncio.Lock.
        """
        if server_id not in self._locks:
            self._locks[server_id] = asyncio.Lock()
        return self._locks[server_id]

    async def discover_tools(self, server_id: str) -> list[ToolDefinition]:
        """Discover tools exposed by an approved MCP server and convert them to ToolDefinitions.

        Args:
            server_id: Identifier of an owner-allowlisted MCP server.

        Returns:
            List of ToolDefinition objects representing tools exposed by the server.

        Raises:
            PermissionError: If server_id is not present in the owner allowlist or is disabled.
            RuntimeError: If communication or discovery fails.
        """
        config = self._verify_server_config(server_id)

        response = await self._send_rpc(config, "tools/list", {})
        raw_tools = response.get("tools", [])

        definitions: list[ToolDefinition] = []
        for raw in raw_tools:
            tool_name = raw.get("name", "")
            if not tool_name:
                continue
            canonical_name = f"mcp.{server_id}.{tool_name}"
            description = raw.get("description", "")
            input_schema = raw.get("inputSchema", {})
            output_schema = raw.get("outputSchema", {"type": "object"})

            definitions.append(
                ToolDefinition(
                    name=canonical_name,
                    version="1.0.0",
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    risk=config.default_risk,
                    confirmation_required=config.confirmation_required,
                    timeout_seconds=config.timeout_seconds,
                    permissions=(f"mcp:{server_id}",),
                    module="mcp",
                )
            )

        return definitions

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """Execute an approved tool on an MCP server and sanitize the untrusted returned content.

        Args:
            server_id: Approved MCP server identifier.
            tool_name: Name of the tool on the MCP server.
            arguments: Validated arguments dictionary.

        Returns:
            ToolResult containing structured outcome and timing.

        Raises:
            PermissionError: If the server is not allowlisted or disabled.
        """
        config = self._verify_server_config(server_id)

        try:
            rpc_result = await self._send_rpc(
                config,
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"MCP tool call failed: {str(exc)}",
                execution_time_ms=0.0,
            )

        # Untrusted content handling: parse content blocks defensively
        is_error = rpc_result.get("isError", False)
        content_blocks = rpc_result.get("content", [])

        texts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))

        joined_text = "\n".join(texts)

        if is_error:
            return ToolResult(
                success=False,
                error=joined_text or "MCP tool reported an error without message.",
                data={"raw": rpc_result},
                execution_time_ms=0.0,
            )

        return ToolResult(
            success=True,
            data={"text": joined_text, "content": content_blocks},
            error=None,
            execution_time_ms=0.0,
        )

    def _verify_server_config(self, server_id: str) -> MCPServerConfig:
        """Verify that a server configuration exists in the owner allowlist and is active.

        Args:
            server_id: Identifier of the requested MCP server.

        Returns:
            Verified MCPServerConfig.

        Raises:
            PermissionError: If server is unknown, unapproved, or disabled.
        """
        config = self.allowlist.get_server(server_id)
        if config is None:
            raise PermissionError(
                f"MCP server '{server_id}' is not in the owner allowlist. Arbitrary server execution denied."
            )
        if not config.enabled:
            raise PermissionError(f"MCP server '{server_id}' is disabled by owner policy.")
        return config

    async def _send_rpc(
        self,
        config: MCPServerConfig,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch a JSON-RPC 2.0 request over the configured transport (stdio or SSE).

        Args:
            config: Verified server configuration.
            method: JSON-RPC method name.
            params: Parameters dictionary.

        Returns:
            The 'result' field of the JSON-RPC response.

        Raises:
            RuntimeError: If the remote server returns a JSON-RPC error or communication fails.
        """
        if config.transport == "stdio":
            return await self._send_stdio_rpc(config, method, params)
        elif config.transport == "sse":
            return await self._send_sse_rpc(config, method, params)
        else:
            raise ValueError(f"Unsupported transport: {config.transport}")

    async def _send_stdio_rpc(
        self,
        config: MCPServerConfig,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a JSON-RPC request over a pinned stdio child process.

        Args:
            config: Verified server configuration with immutable command and arguments.
            method: JSON-RPC method name.
            params: Parameters dictionary.

        Returns:
            Decoded result dictionary.
        """
        lock = self._get_lock(config.id)
        async with lock:
            proc = await self._ensure_stdio_process(config)
            if proc.stdin is None or proc.stdout is None:
                raise RuntimeError(f"Process pipes unavailable for stdio MCP server '{config.id}'")

            request_id = str(uuid4())
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            line = json.dumps(payload) + "\n"
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()

            raw_response = await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=config.timeout_seconds,
            )
            if not raw_response:
                raise RuntimeError(f"MCP server '{config.id}' closed standard output unexpectedly")

            decoded = json.loads(raw_response.decode("utf-8").strip())
            if "error" in decoded:
                err = decoded["error"]
                raise RuntimeError(f"MCP RPC error from '{config.id}': {err.get('message', err)}")

            return decoded.get("result", {})

    async def _ensure_stdio_process(self, config: MCPServerConfig) -> asyncio.subprocess.Process:
        """Ensure a running initialized stdio subprocess for an approved server.

        Args:
            config: Verified server configuration.

        Returns:
            Active asyncio subprocess.

        Raises:
            ValueError: If command is missing.
            RuntimeError: If process spawn or handshake fails.
        """
        proc = self._stdio_processes.get(config.id)
        if proc is not None and proc.returncode is None:
            return proc

        if not config.command:
            raise ValueError(f"Stdio server '{config.id}' has no pinned executable command.")

        # Spawn pinned subprocess; arbitrary command injection is impossible because command and args
        # are sourced directly from the verified owner allowlist configuration.
        safe_env = {**os.environ, **config.env}
        proc = await asyncio.create_subprocess_exec(
            config.command,
            *config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )
        self._stdio_processes[config.id] = proc

        # Perform MCP initialize handshake
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError(f"Subprocess pipes for '{config.id}' could not be established.")

        init_payload = {
            "jsonrpc": "2.0",
            "id": "init-1",
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "bbd-os", "version": "0.1.0"},
            },
        }
        proc.stdin.write((json.dumps(init_payload) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        init_line = await asyncio.wait_for(proc.stdout.readline(), timeout=config.timeout_seconds)
        if not init_line:
            raise RuntimeError(f"MCP server '{config.id}' did not reply to initialize request.")

        # Send notifications/initialized
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        return proc

    async def _send_sse_rpc(
        self,
        config: MCPServerConfig,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch a JSON-RPC request to an approved HTTP/SSE MCP server.

        Args:
            config: Verified server configuration with validated URL.
            method: JSON-RPC method name.
            params: Parameters dictionary.

        Returns:
            Result payload from the remote server.

        Raises:
            ValueError: If URL is missing or invalid.
            RuntimeError: If network request or protocol returns an error.
        """
        if not config.url:
            raise ValueError(f"SSE server '{config.id}' has no configured URL.")

        parsed = urlsplit(config.url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme '{parsed.scheme}' for MCP server '{config.id}'.")

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid4()),
            "method": method,
            "params": params,
        }

        async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
            resp = await client.post(
                config.url,
                json=payload,
                headers={"Content-Type": "application/json", **config.headers},
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                err = data["error"]
                raise RuntimeError(f"MCP RPC error from '{config.id}': {err.get('message', err)}")
            return data.get("result", {})

    async def close(self) -> None:
        """Terminate all open stdio child processes cleanly."""
        for server_id, proc in list(self._stdio_processes.items()):
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    proc.kill()
        self._stdio_processes.clear()
