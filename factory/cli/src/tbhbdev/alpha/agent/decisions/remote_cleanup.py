"""Remote run retention and cleanup scripts."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.decisions.remote_shell import (
    HELPERS,
    OWNER_MATCH,
    owned,
    owner_input,
    socket_root,
)


def keep(name: str) -> str:
    path = f'"$d/{name}{files.KEEP}"'
    return (
        owned(name)
        + HELPERS
        + f"""umask 077
safe_file {path}
set -C
if test ! -f {path}; then ( : > {path} ); fi
"""
    )


# A supervisor may hold the lock outside tmux, or leave a detached group.
OWNERLESS_LIVENESS = r"""
my ($lock, $group) = @ARGV;
if (-f $lock) {
    open(my $fh, "<", $lock) or exit 65;
    flock($fh, LOCK_EX | LOCK_NB) or exit 1;
}
if (-f $group) {
    open(my $fh, "<", $group) or exit 65;
    defined(read($fh, my $data, 4097)) && length($data) <= 4096 or exit 65;
    my $record = eval { decode_json($data) };
    ref($record) eq "HASH" or exit 65;
    my $pgid = $record->{pgid} // "";
    $pgid =~ /\A[0-9]{1,10}\z/ && $pgid > 1 && $pgid <= 2147483647 or exit 65;
    exit 1 if kill(0, -$pgid) || $! != ESRCH;
}
"""


def clean(  # noqa: PLR0913 -- explicit remote cleanup inputs
    repository: str,
    name: str,
    stamp: str,
    sockets_dir: str,
    *,
    force: bool,
    ownerless_only: bool = False,
) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    tree = f".worktrees/agent-{name}"
    move = """check_directory() {
    if test -L "$1"; then
        printf '%s\\n' "clean refused: $1 is a symlink" >&2; exit 65
    fi
    if test -e "$1" && test ! -d "$1"; then
        printf '%s\\n' "clean refused: $1 is not a directory" >&2; exit 65
    fi
}
"""
    for component in (clone, clone / ".worktrees", clone / tree):
        move += f'check_directory "$HOME"/{shlex.quote(str(component))}\n'
    move += f"""path="$HOME"/{shlex.quote(str(clone / tree))}
if test -d "$path"; then
    test ! -e "$trash/worktree" && test ! -L "$trash/worktree" || exit 65
    mv "$path" "$trash/worktree" || exit "$?"
fi
"""
    command = (
        owner_input(name)
        + 'orphan=0\nif test ! -e "$owner" && test ! -L "$owner"; then\n'
        + "    orphan=1\nelse\n"
        + ("    exit 73\n" if ownerless_only else f"    {OWNER_MATCH}")
        + "fi\n"
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
if test "$orphan" -eq 1; then
    lock="$d/{name}{files.RUN_LOCK}"
    group="$d/{name}{files.PGID}"
    safe_file "$lock"
    safe_file "$group"
    if test -f "$lock" || test -f "$group"; then
        perl -MFcntl=:flock -MJSON::PP -MErrno=ESRCH \\
            -e {shlex.quote(OWNERLESS_LIVENESS)} "$lock" "$group" || exit "$?"
    fi
    # Claim the name just as transfer does, and keep it until all moves finish.
    mkdir -p -m 700 "$d" || exit "$?"
    (set -C; printf '%s\\n' "$given" > "$owner") || exit 73
    trap 'rm -f "$owner" || exit "$?"' EXIT
    trap 'exit 1' HUP INT TERM
fi
"""
    )
    if not force:
        command += f"""fallback="$d/{name}{files.FALLBACK}"
if test "$orphan" -eq 0 && {{ test -e "$fallback" || test -L "$fallback"; }}; then
    printf '%s\\n' 'clean refused: fallback rc requires --force' >&2
    exit 65
fi
"""
    command += (
        f"""trash="$d/trash/{stamp}-{name}"
test ! -L "$d/trash" && test ! -L "$trash" || exit 65
mkdir -p "$trash" || exit "$?"
"""
        + move
    )
    for suffix in files.SUFFIXES:
        if suffix == files.OWNER:
            command += 'test "$orphan" -eq 0 || exit 0\n'
        path = f'"$d/{name}{suffix}"'
        command += (
            f"safe_file {path}\n"
            f"if test -f {path}; then\n"
            f'    target="$trash/{name}{suffix}"\n'
            '    test ! -e "$target" && test ! -L "$target" || exit 65\n'
            f'    mv {path} "$target" || exit "$?"\n'
            "fi\n"
        )
    return command
