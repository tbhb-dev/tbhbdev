"""Synchronize a version mismatch once before the driver launches a run."""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from pwd import getpwuid
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from tbhbdev import version
from tbhbdev.alpha import version_check
from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote_host, remote_shell
from tbhbdev.alpha.agent.driver.decisions.hosts import select_host
from tbhbdev.alpha.agent.driver.decisions.launch import check_retry, same_version
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.install import cli_build

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import NoReturn

    from tbhbdev.alpha.agent.model import Host, Settings

_RETRY = "_TBHB_AGENT_VERSION_HEAL"
_INSTALL_TIMEOUT = 300
_PROBE_TIMEOUT = 30
_FAILED = "install failed"
_PROBE_MARKER = "# tbhbdev-heal-probe\n"
# TemporaryDirectory creates the private cwd under this fixed parent.
_TEMP_ROOT = "/tmp"  # noqa: S108  # nosec B108
_NETWORK_ENV = ("HTTPS_PROXY", "SSL_CERT_FILE", "GIT_SSL_CAINFO")
_ATTEMPTED: dict[tuple[str, str], tuple[str | None, str | None]] = {}


def reset() -> None:
    """Clear the per-invocation host heal record."""
    _ATTEMPTED.clear()


def heal_env(cwd: str) -> dict[str, str]:
    home = getpwuid(os.getuid()).pw_dir
    return {
        "HOME": home,
        "PATH": f"{version_check.TOOL_PATH}:{home}/.local/bin",
        "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        **{key: os.environ[key] for key in _NETWORK_ENV if key in os.environ},
    }


def read_origin(argv: list[str]) -> tuple[int, str]:
    flags = ("-c", "credential.helper=", "-c", "http.extraHeader=")
    try:
        with TemporaryDirectory(dir=_TEMP_ROOT) as cwd:
            result = subprocess.run(  # nosec B603
                [argv[0], *flags, *argv[1:]],
                cwd=cwd,
                env=heal_env(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                errors="replace",
                timeout=version_check.TIMEOUT,
                check=False,
            )
    except OSError, subprocess.TimeoutExpired:
        return 1, ""
    return result.returncode, result.stdout


def retrying() -> bool:
    return _RETRY in os.environ


def restart(mismatch: str) -> NoReturn:
    os.environ[_RETRY] = mismatch
    try:
        entry = Path(getpwuid(os.getuid()).pw_dir) / ".local/bin/tbhbdev"
        os.execv(entry, [str(entry), *sys.argv[1:]])  # noqa: S606  # nosec B606
    finally:
        os.environ.pop(_RETRY, None)


def report_install(summary: str, *, failed: bool) -> None:
    if failed:
        raise OSError(summary or _FAILED)
    print(f"version heal: {summary}", file=sys.stderr, flush=True)


def _remaining(deadline: float | None, limit: float) -> float:
    if deadline is None:
        return limit
    left = deadline - time.monotonic()
    if left <= 0:
        message = "launch deadline exceeded"
        raise TimeoutError(message)
    return min(limit, left)


def _installed(settings: Settings, host: Host, *, timeout: float) -> str:
    return transport.shell(
        settings, host, _PROBE_MARKER + remote_host.installed(host), timeout=timeout
    ).strip()


def sync(
    settings: Settings,
    host: Host,
    local: str,
    installed: str,
    *,
    deadline: float | None = None,
) -> bool:
    """Install origin main on each side that differs; restart if driver updates."""
    if local == installed:
        return False
    _remaining(deadline, version_check.TIMEOUT)
    head = version_check.latest(read_origin)
    if head is None:
        message = f"cannot read main of {version_check.REPO_URL}"
        raise LookupError(message)
    host_commit = version_check.parse_stamp(installed) or version.UNKNOWN
    driver_commit = version_check.parse_stamp(local) or version.UNKNOWN
    if host_commit == driver_commit == head:
        message = "host and driver already match origin main"
        raise LookupError(message)
    install = cli_build.script(head, version_check.REPO_URL)
    if host_commit != head:
        # SSH drops stdout on a nonzero exit. Carry the status in-band.
        summary = transport.shell(
            settings,
            host,
            remote_shell.PREFIX + f"(\n{install}) || printf '%s\\n' '{_FAILED}'\n",
            timeout=_remaining(deadline, _INSTALL_TIMEOUT),
        ).strip()
        report_install(
            summary.removesuffix(_FAILED).strip(), failed=summary.endswith(_FAILED)
        )
    if driver_commit != head:
        if retrying():
            message = "driver already restarted once"
            raise OSError(message)
        with TemporaryDirectory(dir=_TEMP_ROOT) as cwd:
            result = subprocess.run(  # nosec B603
                ["/bin/sh", "-c", install],
                cwd=cwd,
                env=heal_env(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                errors="replace",
                timeout=_remaining(deadline, _INSTALL_TIMEOUT),
                check=False,
            )
        report_install(result.stdout.strip(), failed=result.returncode != 0)
        restart(f"version mismatch: driver {local!r}; host {installed!r}")
    return True


def heal_candidate(
    settings: Settings,
    host: Host,
    *,
    timeout: float,
    deadline: float | None = None,
) -> tuple[tuple[str | None, str | None] | None, bool]:
    """Heal HOST once before limits; return ((mismatch, error) or None, synced)."""
    key = (host.name, host.account)
    if (prior := _ATTEMPTED.get(key)) is not None:
        return (prior if prior[1] is not None else None), False
    local, mismatch_text = version.line(), None
    try:
        installed = _installed(settings, host, timeout=timeout)
        try:
            same_version(local, installed)
        except Invalid as mismatch:
            mismatch_text = str(mismatch)
            end = time.monotonic() + timeout if deadline is None else deadline
            synced = sync(settings, host, local, installed, deadline=end)
            _ATTEMPTED[key] = (mismatch_text, None)
            return None, synced
    except (LookupError, OSError, subprocess.SubprocessError) as exc:
        _ATTEMPTED[key] = (mismatch_text, str(exc))
        return _ATTEMPTED[key], False
    _ATTEMPTED[key] = (None, None)
    return None, False


def heal_hosts(settings: Settings, names: Sequence[str]) -> int:
    """Sync each named host to origin main and print before/after versions."""
    targets = [select_host(settings, name, None) for name in names]
    local, failed = version.line(), False
    try:
        for host in targets:
            try:
                before = _installed(settings, host, timeout=_PROBE_TIMEOUT)
                after = before
                if before != local:
                    sync(settings, host, local, before)
                    after = _installed(settings, host, timeout=_PROBE_TIMEOUT)
            except (LookupError, OSError, subprocess.SubprocessError) as exc:
                print(f"{host.name}: heal failed: {exc}")
                failed = True
            else:
                print(f"{host.name}: before {before} after {after}")
    finally:
        os.environ.pop(_RETRY, None)
    return 1 if failed else 0


def check(settings: Settings, host: Host, local: str, installed: str) -> str | None:
    """Check parity and return the original error if the host needs a reread."""
    previous = os.environ.pop(_RETRY, None)
    if previous is not None:
        check_retry(local, installed, previous)
        return None
    try:
        same_version(local, installed)
    except Invalid as mismatch:
        key = (host.name, host.account)
        prior = _ATTEMPTED.get(key)
        if prior is not None and prior[0] is not None:
            check_retry(local, installed, prior[0])
            return None
        try:
            sync(settings, host, local, installed)
        except LookupError:
            raise mismatch from None
        except (OSError, subprocess.SubprocessError) as exc:
            _ATTEMPTED[key] = (str(mismatch), str(exc))
            message = f"{mismatch} (version sync failed: {exc})"
            raise Invalid(message) from None
        _ATTEMPTED[key] = (str(mismatch), None)
        return str(mismatch)
    return None
