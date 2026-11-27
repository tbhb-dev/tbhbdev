"""Check the versions of tools used by commits and pull requests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

VERSIONS = Path(__file__).with_name("pr-tool-versions.json")
PINNED = frozenset({"cspell", "gitleaks", "rumdl", "scc", "vale"})
CHECK_TOOLS = {
    "node": "node",
    "cspell": "cspell",
    "rumdl": "rumdl",
    "aqua:boyter/scc": "scc",
    "vale": "vale",
    "gitleaks": "gitleaks",
    "cocogitto": "cog",
    "ruff": "ruff",
    "shfmt": "shfmt",
    "shellcheck": "shellcheck",
    "buildifier": "buildifier",
    "tombi": "tombi",
    "github:owenlamont/ryl": "ryl",
    "actionlint": "actionlint",
    "editorconfig-checker": "editorconfig-checker",
}


def version_problem(name: str, expected: str, output: str) -> str | None:
    """Explain why a bare or tool-prefixed version report fails the pin."""
    match = re.search(
        rf"^(?:{re.escape(name)} )?(?:version:? )?v?(\d[\w.+-]*)"
        r"(?: \([^\n]*\))?[ \t]*$",
        output.strip(),
        re.MULTILINE,
    )
    version = match[1] if match else None
    if version == expected:
        return None
    return f"reports {version or 'no version'}, expected pinned version {expected}"


def read_pins(top: Path) -> dict[str, str]:
    """Read the check-tool entries in the repository's mise configuration."""
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(top / "mise.toml", flags), "rb") as config:
            tools = tomllib.load(config).get("tools", {})
    except FileNotFoundError:
        return {}
    if not isinstance(tools, dict):
        message = "mise.toml [tools] must be a table"
        raise TypeError(message)
    table = cast("dict[str, object]", tools)
    pins: dict[str, str] = {}
    for key in CHECK_TOOLS:
        if key not in table:
            continue
        value = table[key]
        if not isinstance(value, str) or not re.fullmatch(r"\d[\w.+-]*", value):
            message = f"mise.toml must specify an exact version for {key}"
            raise ValueError(message)
        pins[key] = value
    return pins


def pin_problems(pins: Mapping[str, str], reports: Mapping[str, str]) -> dict[str, str]:
    """Compare the configured pins with reports from the account's PATH."""
    problems: dict[str, str] = {}
    for key, expected in pins.items():
        name = CHECK_TOOLS[key]
        if name not in reports:
            problems[key] = f"{name} missing, expected pinned version {expected}"
        elif problem := version_problem(name, expected, reports[name]):
            problems[key] = f"{name} {problem}"
    return problems


def version_reports(
    pins: Mapping[str, str], env: Mapping[str, str], cwd: Path
) -> dict[str, str]:
    reports: dict[str, str] = {}
    for key in pins:
        name = CHECK_TOOLS[key]
        if executable := shutil.which(name, path=env["PATH"]):
            try:
                reports[name] = reported_version(executable, env=env, cwd=cwd)
            except OSError, UnicodeError:
                reports[name] = ""
    return reports


def check_tools(
    top: Path, env: Mapping[str, str], cwd: Path
) -> tuple[dict[str, str], dict[str, str]]:
    """Return the check-tool pins and any differences on the account's PATH."""
    pins = read_pins(top)
    return pins, pin_problems(pins, version_reports(pins, env, cwd))


def tool_command(
    args: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # nosec B603
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(str(exc)) from exc


def reported_version(
    path: str, *, env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> str:
    done = tool_command([path, "--version"], env=env, cwd=cwd)
    return done.stdout if done.returncode == 0 else ""


def pinned_version(name: str) -> str:
    try:
        versions = cast(
            "dict[str, object]", json.loads(VERSIONS.read_text(encoding="utf-8"))
        )
        version = versions[name]
    except (KeyError, OSError, TypeError, ValueError) as exc:
        message = f"cannot read the {name} version pin from {VERSIONS}"
        raise SystemExit(message) from exc
    if not isinstance(version, str) or not version:
        message = f"invalid {name} version pin in {VERSIONS}"
        raise SystemExit(message)
    return version


def require_version(name: str, path: str) -> str:
    """Return the selected path or stop if its version cannot be verified."""
    if name not in PINNED:
        return path
    expected = pinned_version(name)
    executable = Path(path).absolute()
    try:
        output = reported_version(str(executable))
    except (OSError, UnicodeError) as exc:
        message = f"cannot check {name} version at {executable} (pin {expected}): {exc}"
        raise SystemExit(message) from exc
    if problem := version_problem(name, expected, output):
        message = f"{name} at {executable} {problem}"
        raise SystemExit(message)
    return str(executable)
