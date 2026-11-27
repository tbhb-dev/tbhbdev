"""Tests for the ops MCP server."""

from importlib import metadata

import anyio
from mcp import Client
from mcp.types import CallToolResult, TextContent, Tool
from tbhbops_mcp import server


def test_server_identifies_itself() -> None:
    async def probe() -> tuple[str | None, str | None]:
        async with Client(server.build_server()) as client:
            info = client.server_info
            return (info.name if info else None, client.instructions)

    assert anyio.run(probe) == ("tbhbops", server.INSTRUCTIONS)


def test_server_offers_a_read_only_local_version_tool() -> None:
    async def probe() -> list[Tool]:
        async with Client(server.build_server()) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(probe)
    assert [t.name for t in tools] == ["version"]
    assert (
        tools[0].description
        == "Return the installed version of the tbhbops-mcp server."
    )
    annotations = tools[0].annotations
    assert annotations is not None
    assert (
        annotations.read_only_hint,
        annotations.idempotent_hint,
        annotations.open_world_hint,
    ) == (True, True, False)


def test_version_tool_returns_the_installed_version() -> None:
    async def probe() -> CallToolResult:
        async with Client(server.build_server()) as client:
            return await client.call_tool("version")

    result = anyio.run(probe)
    expected = metadata.version("tbhbops-mcp")
    assert not result.is_error
    assert result.content == [TextContent(type="text", text=expected)]
    assert result.structured_content == {"result": expected}
