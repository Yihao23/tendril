"""Bridge MCP servers into the tool registry, so Tendril can reach any app that has an MCP server."""

from __future__ import annotations

import logging
import os
import re
from contextlib import AsyncExitStack
from typing import Any

from .base import Registry, Tool, ToolContext

log = logging.getLogger("tendril.mcp")


def _tool_name(server: str, tool: str) -> str:
    # OpenAI-style function names: ^[a-zA-Z0-9_-]{1,64}$
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]


def _render(result: Any) -> str:
    parts = []
    for item in result.content:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else f"[{item.type} content omitted]")
    text = "\n".join(parts) or "(no output)"
    return f"MCP tool error: {text}" if result.isError else text


class MCPManager:
    def __init__(self, servers: dict[str, dict]):
        self.servers = servers
        self.stack = AsyncExitStack()
        self.errors: dict[str, str] = {}

    async def start(self, registry: Registry) -> None:
        if not self.servers:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:
            log.warning("MCP servers are configured but the `mcp` package is missing: pip install 'tendril[mcp]'")
            return

        for name, cfg in self.servers.items():
            try:
                if "url" in cfg:
                    read, write, _ = await self.stack.enter_async_context(
                        streamablehttp_client(cfg["url"], headers=cfg.get("headers"))
                    )
                else:
                    server = StdioServerParameters(
                        command=cfg["command"], args=cfg.get("args", []), env={**os.environ, **cfg.get("env", {})}
                    )
                    read, write = await self.stack.enter_async_context(stdio_client(server))
                session = await self.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
            except Exception as e:  # one broken server shouldn't take the agent down
                self.errors[name] = repr(e)
                log.warning("MCP server %s failed to start: %r", name, e)
                continue

            for t in listed.tools:
                read_only = bool(t.annotations and t.annotations.readOnlyHint)
                registry.add(Tool(
                    name=_tool_name(name, t.name),
                    description=f"[{name}] {t.description or t.name}",
                    parameters=t.inputSchema or {"type": "object", "properties": {}},
                    handler=self._caller(session, t.name),
                    default_policy="allow" if read_only else "ask",
                    source=f"mcp:{name}",
                ))
            log.info("MCP server %s: %d tools", name, len(listed.tools))

    @staticmethod
    def _caller(session: Any, tool_name: str):
        async def call(ctx: ToolContext, args: dict) -> str:
            return _render(await session.call_tool(tool_name, args))

        return call

    async def close(self) -> None:
        await self.stack.aclose()
