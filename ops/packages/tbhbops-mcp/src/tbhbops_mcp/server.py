"""The ops MCP server and the tools it offers."""

from importlib import metadata

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

NAME = "tbhbops"
DISTRIBUTION = "tbhbops-mcp"
INSTRUCTIONS = (
    "Tools for operating the infrastructure that tbhb-dev/tbhbdev manages. "
    "The server runs locally over stdio."
)

# Reads local package metadata only: no side effects, the same answer on
# every call, and nothing outside this process.
READ_ONLY_LOCAL = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)


def version() -> str:
    """Return the installed version of the tbhbops-mcp server."""
    return metadata.version(DISTRIBUTION)


def build_server() -> MCPServer:
    """Return the ops MCP server with every tool registered."""
    server = MCPServer(NAME, instructions=INSTRUCTIONS)
    server.tool(annotations=READ_ONLY_LOCAL)(version)
    return server
