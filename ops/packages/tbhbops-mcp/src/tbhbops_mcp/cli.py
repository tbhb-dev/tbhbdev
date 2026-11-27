"""Command line for the ops MCP server.

Usage: tbhbops-mcp [--version]

Serves MCP over stdio until the client closes the connection. The server
has no network transport, so nothing outside the launching harness can
reach it.
"""

import argparse

from tbhbops_mcp.server import DISTRIBUTION, build_server, version


def main(argv: list[str] | None = None) -> int:
    """Parse ARGV, then serve over stdio and return the exit status."""
    parser = argparse.ArgumentParser(
        prog=DISTRIBUTION,
        description="Serve the tbhb-dev/tbhbdev MCP tools over stdio.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    parser.parse_args(argv)
    build_server().run("stdio")
    return 0
