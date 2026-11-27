"""Compare the account's check tools with the pins at the run's checkout."""

import re
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from fake_tool import write_fake

from tbhbdev.alpha import tool_pins
from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("reports", "problem"),
    [
        ({}, "rumdl missing, expected pinned version 1.2.3"),
        (
            {"rumdl": "rumdl 1.2.2"},
            "rumdl reports 1.2.2, expected pinned version 1.2.3",
        ),
        (
            {"rumdl": "rumdl 1.2.4"},
            "rumdl reports 1.2.4, expected pinned version 1.2.3",
        ),
        ({"rumdl": "rumdl 1.2.3"}, ""),
    ],
)
def test_pin_comparison(reports: dict[str, str], problem: str) -> None:
    assert tool_pins.pin_problems({"rumdl": "1.2.3"}, reports) == (
        {"rumdl": problem} if problem else {}
    )


@pytest.mark.parametrize(
    ("name", "report"),
    [
        ("node", "v1.2.3"),
        ("cspell", "1.2.3"),
        ("scc", "scc version 1.2.3"),
        ("shellcheck", "ShellCheck\nversion: 1.2.3\nlicense: GNU"),
        ("buildifier", "buildifier version: 1.2.3 \nbuildifier scm revision: abc"),
        ("tombi", "tombi 1.2.3 (platform)"),
    ],
)
def test_version_formats(name: str, report: str) -> None:
    assert tool_pins.version_problem(name, "1.2.3", report) is None


@pytest.mark.parametrize("config", ['tools = ["node"]', '[tools]\nnode = "latest"'])
def test_invalid_pin_is_rejected(tmp_path: Path, config: str) -> None:
    (tmp_path / "mise.toml").write_text(config)
    with pytest.raises(Invalid, match=r"could not read the check tool pins"):
        host_tools.require_check_tools(tmp_path, {"PATH": str(tmp_path)})


def test_target_refusal_lists_every_problem_on_one_line(tmp_path: Path) -> None:
    (tmp_path / "mise.toml").write_text(
        '[tools]\nnode = "1.2.3"\nvale = "2.3.4"\ngo = "3.4.5"\n'
    )
    write_fake(tmp_path / "vale", "print('vale version 2.3.3')\n")
    message = (
        "pinned check tools: node missing, expected pinned version 1.2.3, "
        "vale reports 2.3.3, expected pinned version 2.3.4. "
        "Run `tbhbdev alpha install` from a checkout at the run's starting ref."
    )
    with pytest.raises(Invalid, match=f"^{re.escape(message)}$"):
        host_tools.require_check_tools(tmp_path, {"PATH": str(tmp_path)})
    write_fake(tmp_path / "node", "print('v1.2.3')\n")
    write_fake(tmp_path / "vale", "print('vale version 2.3.4')\n")
    host_tools.require_check_tools(tmp_path, {"PATH": str(tmp_path)})


def test_version_probe_handles_unusable_executable(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.write_text("#!/missing/interpreter\n")
    node.chmod(0o755)
    assert tool_pins.version_reports(
        {"node": "1.2.3"}, {"PATH": str(tmp_path)}, tmp_path
    ) == {"node": ""}


def test_tool_command_bounds_a_hung_program() -> None:
    with pytest.raises(TimeoutError) as caught:
        tool_pins.tool_command(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0
        )
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
