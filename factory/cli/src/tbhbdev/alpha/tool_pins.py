"""Check PR tools against pinned versions or verify they report a version."""

import json
import re
import subprocess  # nosec B404
from pathlib import Path
from typing import cast

VERSIONS = Path(__file__).with_name("pr-tool-versions.json")
PINNED = frozenset({"cspell", "gitleaks", "rumdl", "scc", "vale"})


def _parse_version(name: str, output: str) -> str | None:
    match = re.fullmatch(
        rf"(?:{re.escape(name)} (?:version )?)?v?(\d\S*)", output.strip()
    )
    return match[1] if match else None


def version_problem(name: str, expected: str, output: str) -> str | None:
    """Explain why a bare or tool-prefixed version report fails the pin."""
    version = _parse_version(name, output)
    if version == expected:
        return None
    return f"reports {version or 'no version'}, expected pinned version {expected}"


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


def _run_version(name: str, executable: Path, *, expected: str | None = None) -> str:
    try:
        done = subprocess.run(  # nosec B603
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        pin = f" (pin {expected})" if expected is not None else ""
        message = f"cannot check {name} version at {executable}{pin}: {exc}"
        raise SystemExit(message) from exc
    return done.stdout if done.returncode == 0 else ""


def require_reported_version(name: str, path: str) -> str:
    """Return the selected path or stop if it cannot report a version."""
    if name not in PINNED:
        return path
    executable = Path(path).absolute()
    output = _run_version(name, executable)
    if _parse_version(name, output) is None:
        message = f"{name} at {executable} reports no version"
        raise SystemExit(message)
    return str(executable)


def require_version(name: str, path: str) -> str:
    """Return the selected path or stop if its version cannot be verified."""
    if name not in PINNED:
        return path
    expected = pinned_version(name)
    executable = Path(path).absolute()
    output = _run_version(name, executable, expected=expected)
    if problem := version_problem(name, expected, output):
        message = f"{name} at {executable} {problem}"
        raise SystemExit(message)
    return str(executable)
