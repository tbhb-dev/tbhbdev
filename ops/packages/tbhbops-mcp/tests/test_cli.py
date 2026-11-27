"""Tests for the tbhbops-mcp command line."""

import os
import shutil
lazy from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from tbhbops_mcp import cli


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out == "tbhbops-mcp 0\n"


def test_help_describes_the_server(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: tbhbops-mcp ")
    assert "Serve the tbhb-dev/tbhbdev MCP tools over stdio." in out.splitlines()


def test_unknown_arguments_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--port", "8080"])
    assert exited.value.code == 2
    assert "unrecognized arguments: --port 8080" in capsys.readouterr().err


def test_main_serves_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[str] = []

    class Fake:
        def run(self, transport: str) -> None:
            served.append(transport)

    monkeypatch.setattr(cli, "build_server", Fake)
    assert cli.main([]) == 0
    assert served == ["stdio"]


def test_console_script_speaks_mcp_over_stdio(tmp_path: Path) -> None:
    # Bazel supplies its console script as a runfile. uv adds its script to PATH.
    script = os.environ.get("TBHBOPS_MCP_TEST_BIN") or shutil.which("tbhbops-mcp")
    assert script is not None

    async def probe() -> str | None:
        # The client's default environment is a short allowlist, which drops
        # the PYTHONPATH that tbhb_rules_python's environment relies on.
        params = StdioServerParameters(command=script, env=dict(os.environ))
        # The server's stderr goes to a file: mutmut replaces sys.stderr
        # with a stream the client's log pump cannot write to.
        with (tmp_path / "server.log").open("w") as errlog:
            async with Client(stdio_client(params, errlog)) as client:
                info = client.server_info
                return info.name if info else None

    assert anyio.run(probe) == "tbhbops"
