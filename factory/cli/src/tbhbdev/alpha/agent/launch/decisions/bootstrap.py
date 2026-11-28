"""Commands for receiving, starting, and removing the CLI bundle."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions.remote_shell import PREFIX, ROOT, owned, socket_root
from tbhbdev.alpha.agent.model import NAME, Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, Profile

_ENTRY_POINTS = r""""$cli/venv/bin/python" -I - "$cli" <<'PY'
import shlex
import sys
import sysconfig
from pathlib import Path

cli = Path(sys.argv[1])
site = Path(sysconfig.get_path("purelib"))
(site / "tbhbdev-bundle.pth").write_text(f"{cli / 'lib'}\n", encoding="utf-8")
python = shlex.quote(str(cli / "venv/bin/python"))
for name, args in (("tbhbdev", ""), ("gh", " alpha gh-shim")):
    entry = cli / "bin" / name
    entry.write_text(
        f'#!/bin/sh\nexec {python} -I -m tbhbdev{args} "$@"\n', encoding="utf-8"
    )
    entry.chmod(0o700)
PY
"""


def _cli_path(name: str) -> str:
    if not NAME.fullmatch(name):
        message = f"run name {name!r} must match {NAME.pattern}"
        raise Invalid(message)
    return f"$d/{name}.cli"


def receive(name: str, sockets_dir: str, sha256: str, python: str) -> str:
    cli = _cli_path(name)
    return (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 73
mkdir -p "$d" && chmod 700 "$d" || exit "$?"
# Claim the name before extracting any input.
IFS= read -r given || exit 73
test -n "$given" || exit 73
set -C
for p in "$d/{name}."*; do
    if test -e "$p" || test -L "$p"; then exit 73; fi
done
( printf '%s\\n' "$given" > "$d/{name}.owner" ) || exit 73
set -e
tar -xf - -C "$d"
archive="{cli}.tar"
safe_file "$archive"
if command -v sha256sum >/dev/null 2>&1; then
    digest=$(sha256sum < "$archive")
else
    digest=$(shasum -a 256 < "$archive")
fi
test "${{digest%% *}}" = {shlex.quote(sha256)} || exit 65
cli="{cli}"
mkdir "$cli"
trap 'rm -rf "$cli"' EXIT
mkdir "$cli/lib" "$cli/bin"
tar -xf "$archive" -C "$cli/lib"
rm -f "$archive"
uv venv --python {shlex.quote(python)} "$cli/venv"
uv pip install --python "$cli/venv/bin/python" -r "$cli/lib/requirements.txt"
"""
        + _ENTRY_POINTS
        + "trap - EXIT\n"
    )


def start(
    name: str, sockets_dir: str, host_name: str, *, steer: bool = False
) -> tuple[str, ...]:
    cli = _cli_path(name)
    command = shlex.join(
        ("alpha", "agent", "run", name, "--host", host_name, "--owned")
    )
    if steer:
        command += " --steer"
    return (
        "/bin/sh",
        "-c",
        owned(name)
        + socket_root(sockets_dir)
        + f'tmux -S "$s/agents" -f /dev/null new-session -d -s ag-{name} '
        + f'"{cli}/bin/tbhbdev" {command}',
    )


def check_slot(name: str, host: Host, profile: Profile) -> str:
    arguments = shlex.join(
        (host.name, profile, str(host.max_concurrent_runs), "--", *host.accounts)
    )
    return (
        "# tbhbdev-check-slot\n"
        + PREFIX
        + ROOT
        + f'exec "{_cli_path(name)}/bin/tbhbdev" alpha agent check-slot {arguments}\n'
    )


def remove_cli(name: str) -> str:
    return PREFIX + ROOT + f'rm -rf "{_cli_path(name)}"\n'
