"""Check PR tools against the versions pinned by the repository."""

import json
import re
import subprocess  # nosec B404
from pathlib import Path
from typing import cast

VERSIONS = Path(__file__).with_name("pr-tool-versions.json")
PINNED = frozenset({"cspell", "gitleaks", "rumdl", "scc", "vale"})


def version_problem(name: str, expected: str, output: str) -> str | None:
    """Explain why a bare or tool-prefixed version report fails the pin."""
    match = re.fullmatch(
        rf"(?:{re.escape(name)} (?:version )?)?v?(\S+)", output.strip()
    )
    version = match[1] if match else None
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


def require_version(name: str, path: str) -> str:
    """Return the selected path or stop if its version cannot be verified."""
    if name not in PINNED:
        return path
    expected = pinned_version(name)
    executable = Path(path).absolute()
    try:
        done = subprocess.run(  # nosec B603
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        message = f"cannot check {name} version at {executable} (pin {expected}): {exc}"
        raise SystemExit(message) from exc
    output = done.stdout if done.returncode == 0 else ""
    if problem := version_problem(name, expected, output):
        message = f"{name} at {executable} {problem}"
        raise SystemExit(message)
    return str(executable)
