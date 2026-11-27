"""Remote agent claims, sessions, status, and logs."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.decisions.remote_host import program
from tbhbdev.alpha.agent.decisions.remote_shell import FILE_HELPERS, owned, socket_root

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host


def session(name: str, command: str, sockets_dir: str) -> str:
    return owned(name) + socket_root(sockets_dir) + f"tmux_state {command} ag-{name}\n"


def transfer(name: str, sockets_dir: str) -> str:
    return (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 73
mkdir -p "$d" && chmod 700 "$d" || exit "$?"
# The owner, stdin's first line, is the atomic name claim before the archive.
IFS= read -r given || exit 73
test -n "$given" || exit 73
set -C
for p in "$d/{name}."*; do
    if test -e "$p" || test -L "$p"; then exit 73; fi
done
( printf '%s\\n' "$given" > "$d/{name}{files.OWNER}" ) || exit 73
tar -xf - -C "$d"
"""
    )


def start(
    name: str, sockets_dir: str, host: Host, *, steer: bool = False
) -> tuple[str, ...]:
    command = shlex.join(
        ("alpha", "agent", "run-host", name, "--host", host.name, "--owned")
    )
    if steer:
        command += " --steer"
    return (
        "/bin/sh",
        "-c",
        owned(name)
        + socket_root(sockets_dir)
        + f'tmux -S "$s/agents" -f /dev/null new-session -d -s ag-{name} '
        + f"{program(host)} {command}",
    )


def read(
    name: str, sockets_dir: str, *, answer: bool = False, reason: bool = False
) -> str:
    header = (
        owned(name)
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
field "$d/{name}{files.PHASE}" 756e6b6e6f776e
field "$d/{name}{files.RC}" -
safe_file "$d/{name}{files.LAUNCHED}"
if test -f "$d/{name}{files.LAUNCHED}"; then printf '1\\n'; else printf '0\\n'; fi
printf '%s\\n' "$present"
"""
    )
    log = f'"$d/{name}{files.LOG}"'
    if answer:
        return header + answer_text(name)
    if reason:
        return (
            header
            + f"""safe_file {log}
if test -f {log}; then
    awk '/^(push (skipped|refused|failed):|pr not opened:)/ ||
         /^(commit check failed:|no new commit:|review not posted:)/ {{
             last = $0
         }}
         END {{ if (last != "") print substr(last, 1, 4096) }}' {log}
fi
"""
        )
    return header + f"safe_file {log}\nif test -f {log}; then head -c 65536 {log}; fi\n"


def answer_text(name: str) -> str:
    final, output = (f'"$d/{name}{suffix}"' for suffix in (files.FINAL, files.OUT))
    return f"""safe_file {final}
safe_file {output}
if test -f {final}; then tail -c 65536 {final}
elif test -f {output}; then tail -c 65536 {output}; fi
"""


def run_rc(name: str) -> str:
    path = f'"$d/{name}{files.RC}"'
    return f"""safe_file {path}
rc=
if test -f {path}; then rc=$(head -c 5 {path}) || exit "$?"; fi
case "$rc" in ''|[0-9]|[0-9][0-9]|[0-9][0-9][0-9]) ;; *) exit 65 ;; esac
"""


def failed_log(name: str) -> str:
    log = f'"$d/{name}{files.LOG}"'
    return (
        run_rc(name)
        + f"""case "$rc" in ''|0) ;; *)
    safe_file {log}
    if test -f {log}; then tail -c 65536 {log}; fi
    exit "$?"
esac
"""
    )


def run_log(name: str) -> str:
    return owned(name) + FILE_HELPERS + failed_log(name) + answer_text(name)


def fallback(name: str, sockets_dir: str) -> str:
    rc, pending, marker = (
        f'"$d/{name}{suffix}"'
        for suffix in (files.RC, files.RC_PENDING, files.FALLBACK)
    )
    return (
        owned(name)
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
safe_file {rc}
safe_file {pending}
safe_file {marker}
set -C
if test ! -f {rc}; then
    if test ! -f {marker}; then ( : > {marker} ) || exit "$?"; fi
    if test -f {pending}; then
        test ! -L "$d/trash" || exit 65
        mkdir -p "$d/trash" || exit "$?"
        stale=$(mktemp -d "$d/trash/{name}-pending.XXXXXXXX") || exit "$?"
        mv {pending} "$stale/" || exit "$?"
    fi
    printf '137\\n' > {pending} && mv -n {pending} {rc}
fi
"""
    )
