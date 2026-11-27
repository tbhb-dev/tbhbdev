"""Shared shell fragments for remote agent commands."""

from __future__ import annotations

import shlex

from tbhbdev.alpha.agent.decisions import runfiles_names as files

PREFIX = """PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
PATH="$HOME/.local/bin:$PATH"
export PATH
LC_ALL=C
LANG=C
export LC_ALL LANG
if test "$(uname -s)" = Darwin; then
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
export PATH
fi
"""


ROOT = f"""state="${{XDG_STATE_HOME:-$HOME/.local/state}}"
case "$state" in /*) ;; *) state="$HOME/.local/state" ;; esac
for p in "$state" "$state/tbhbdev" "$state/{files.DIRECTORY}"; do
    test ! -L "$p" || exit 65
done
d="$state/{files.DIRECTORY}"
"""


# Copy this boundary until the host and driver branches can share it.
GIT = (
    'env -i PATH="$PATH" HOME="$HOME" LANG=C LC_ALL=C '
    "GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1 "
    "GIT_CONFIG_NOSYSTEM=1 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false "
)


GIT_FLAGS = (
    "git -c core.hooksPath=/dev/null -c core.fsmonitor=false "
    "-c core.sshCommand=ssh -c credential.helper= -c core.askPass= "
    "-c protocol.ext.allow=never -c core.pager=cat "
    "-c gc.auto=0 -c maintenance.auto=false"
)


GIT_SSH = GIT + 'SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}" ' + GIT_FLAGS

FILE_HELPERS = r"""safe_file() {
    test ! -L "$1" || exit 65
    if test -e "$1"; then test -f "$1" || exit 65; fi
}
field() {
    safe_file "$1"
    if test -f "$1"; then
        head -c 64 "$1" | od -An -v -tx1 | tr -d ' \n'
    else printf '%s' "$2"; fi
    printf '\n'
}
"""


HELPERS = (
    FILE_HELPERS
    + r"""tmux_state() {
    error=$(tmux -S "$s/agents" -f /dev/null "$1" -t "=$2" 2>&1)
    code=$?
    present=1
    if test "$code" -eq 0; then return; fi
    test "$code" -eq 1 || exit 75
    case "$error" in
        "can't find session:"*|"no server running on "*) present=0 ;;
        "error connecting to "*" (No such file or directory)") present=0 ;;
        *) exit 75 ;;
    esac
}
"""
)


def socket_root(sockets_dir: str) -> str:
    path = shlex.quote(sockets_dir)
    if not sockets_dir.startswith("/"):
        path = '"$HOME"/' + shlex.quote(sockets_dir)
    return (
        PREFIX
        + ROOT
        + f"s={path}\n"
        + """umask 077
if test ! -e "$s"; then mkdir -p -m 700 "$s" || exit "$?"; fi
test ! -L "$s" && test ! -L "$s/agents" || exit 65
test "$(find "$s" -prune -type d -user "$(id -un)" -perm 0700)" = "$s" || exit 65
"""
        + HELPERS
    )


OWNER_MATCH = (
    'test ! -L "$owner" && test -f "$owner" && '
    'test "$(cat "$owner")" = "$given" || exit 73\n'
)


def owner_input(name: str) -> str:
    return (
        PREFIX
        + ROOT
        + f'owner="$d/{name}{files.OWNER}"\n'
        + 'IFS= read -r given || exit 73\ntest -n "$given" || exit 73\n'
    )


def owned(name: str) -> str:
    """Check the owner on stdin's first line against the run's owner file.

    The owner never appears in the script, because ssh arguments reach audit
    logs. ``transport`` writes the owner as the first line of stdin.
    """
    return owner_input(name) + OWNER_MATCH
