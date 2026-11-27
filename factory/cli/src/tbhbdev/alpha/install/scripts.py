"""Generate Git hook scripts and user-level shell shims."""

import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.install.model import PROG

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import PurePath

SHIM_MARK = "# tbhbdev alpha agent identity hook (tbhbdev alpha install)"
IDENTITY_HOOKS = (
    ("prepare-commit-msg", "trailer"),
    ("commit-msg", "check"),
    ("post-commit", "consume"),
)


SHIM = """#!/bin/sh
{mark}
status=0
if command -v tbhbdev >/dev/null 2>&1; then
  tbhbdev alpha identity {verb} "$@"
  status=$?
else
  origin=$(git remote get-url --push origin 2>/dev/null) || origin=
  if [ -z "$origin" ]; then
    if [ -n "{agent_markers}" ] || [ "${{GIT_COMMITTER_EMAIL:-}}" = '{agent_email}' ] ||
      [ "$(git config --get user.email 2>/dev/null)" = '{agent_email}' ]; then
      echo "{hook}: cannot determine the origin push URL" >&2
      status=1
    fi
  elif printf '%s' "$origin" | LC_ALL=C tr '\\n' ' ' |
    LC_ALL=C grep -Eqi '{origin}'; then
    status=1
  fi
  if [ "$status" -ne 0 ]; then
    echo "{hook}: tbhbdev is not on PATH, so the identity hooks can't run" >&2
    echo "{hook}: install tbhbdev on PATH and run {install_hooks}" >&2
  fi
fi
next="$(dirname "$0")/{hook}.prek"
if [ -x "$next" ]; then
  "$next" "$@" || status=$?
fi
exit $status
"""


PRE_PUSH_SHIM = """#!{python}
{mark}
import os
import pathlib
import re
import shutil
import subprocess
import sys

payload = sys.stdin.buffer.read()
run_env = {run_env!r}
status = 0
if shutil.which("tbhbdev"):
    status = subprocess.run(
        ["tbhbdev", "alpha", "pre-push-guard", *sys.argv[1:]],
        input=payload, env=os.environ | run_env, check=False,
    ).returncode
else:
    target = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    if not target:
        print("pre-push: cannot determine the push destination", file=sys.stderr)
    org_target = re.fullmatch({pattern!r}, target, re.IGNORECASE | re.ASCII)
    if not target or run_env or org_target:
        print("pre-push: tbhbdev is not on PATH; push refused", file=sys.stderr)
        print(
            "pre-push: install tbhbdev on PATH and run "
            "tbhbdev alpha install",
            file=sys.stderr,
        )
        status = 1
next_hook = pathlib.Path(__file__).with_name("pre-push.prek")
if next_hook.is_file():
    result = subprocess.run([str(next_hook), *sys.argv[1:]], input=payload, check=False)
    status = status or result.returncode
sys.exit(status)
"""


def pre_push_shim(python: PurePath, pattern: str, run_env: Mapping[str, str]) -> str:
    """Render the pre-push guard and its chained prek hook."""
    return PRE_PUSH_SHIM.format(
        python=python, mark=SHIM_MARK, pattern=pattern, run_env=dict(run_env)
    )


def identity_shim(
    name: str,
    verb: str,
    origin_ere: str,
    agent_markers: tuple[str, ...],
    agent_email: str,
) -> str:
    """Render a clone's identity hook."""
    return SHIM.format(
        mark=SHIM_MARK,
        verb=verb,
        hook=name,
        origin=f"^{origin_ere}$",
        agent_markers="".join("${" + marker + ":-}" for marker in agent_markers),
        agent_email=agent_email,
        install_hooks=PROG,
    )


SSH_SHIM_MARK = "# tbhbdev alpha install: ssh shim"


# A command-line -o beats the IdentityAgent in the operator's ssh config. The
# shim puts one first (the first -o for an option wins): the caller's
# SSH_AUTH_SOCK unless it is empty or names the 1Password agent, else none. A
# caller who wants another agent sets SSH_AUTH_SOCK. BatchMode stays the
# caller's and the ssh config's. The shim runs the next ssh on PATH that is not
# the shim itself (same file, so a symlink or hard link can't loop it), so a
# host with its ssh elsewhere keeps working.
SSH_SHIM_TEXT = f"""#!/bin/sh
{SSH_SHIM_MARK}
case $0 in */*) here=${{0%/*}} ;; *) here=. ;; esac
here=$(CDPATH= cd "$here" && pwd -P)
real=
oldifs=$IFS
IFS=:
for dir in $PATH; do
  [ -n "$dir" ] || continue
  [ "$(CDPATH= cd "$dir" 2>/dev/null && pwd -P)" = "$here" ] && continue
  [ "$dir/ssh" -ef "$here/${{0##*/}}" ] && continue
  if [ -x "$dir/ssh" ] && [ ! -d "$dir/ssh" ]; then
    real=$dir/ssh
    break
  fi
done
IFS=$oldifs
if [ -z "$real" ]; then
  echo "ssh: no ssh on PATH besides the tbhbdev shim" >&2
  exit 127
fi
sock=${{SSH_AUTH_SOCK:-}}
canonical_sock=
if [ -n "$sock" ]; then
  canonical_sock=$(realpath -- "$sock" 2>/dev/null ||
    readlink -f -- "$sock" 2>/dev/null)
fi
case $sock:$canonical_sock in
  :|*[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]*) sock=none ;;
esac
set -- -o "IdentityAgent=\\"$sock\\"" "$@"
exec "$real" "$@"
"""


def path_export(bin_path: PurePath, current: str) -> str:
    """The text to append to a shell environment file, if needed."""
    line = f'export PATH={shlex.quote(str(bin_path))}":$PATH"'
    if line in current.splitlines():
        return ""
    separator = "\n" if current and not current.endswith("\n") else ""
    return f"{separator}{line}\n"
