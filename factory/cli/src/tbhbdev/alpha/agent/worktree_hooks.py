"""Install and verify the Git hooks for a run clone."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess  # nosec B404
import sys
from functools import partial
from pathlib import Path
lazy from collections.abc import Mapping

from tbhbdev.alpha import pre_push_guard, preflight_gate
from tbhbdev.alpha.agent import host_tools, runfiles, worktree_errors
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.identity.commit.policy import RUN_HOOK_ENV
from tbhbdev.alpha.install.scripts import IDENTITY_HOOKS, SHIM_MARK, pre_push_shim
from tbhbdev.alpha.repo import TBHB_DEV_ORIGIN

RUN_IDENTITY_SHIM = """#!/bin/sh
{mark}
export {run_env}=1
if ! command -v tbhbdev >/dev/null 2>&1; then
  echo "{hook}: tbhbdev is required on PATH for commit identity" >&2
  exit 1
fi
tbhbdev alpha identity {verb} "$@" || exit $?
exec "$(dirname "$0")/{hook}.prek" "$@"
"""
RUN_PREK_SHIM = """#!/bin/sh
[ -f .pre-commit-config.yaml ] || [ -f prek.toml ] || exit 0
hooks=$({prek} list --hook-stage {stage}) || exit $?
[ -n "$hooks" ] || exit 0
exec {prek} hook-impl --hook-type {stage} -- "$@"
"""
RUN_REFERENCE_TRANSACTION = r"""#!/bin/sh
[ "$1" = prepared ] || exit 0
branch={branch}
start={start}
start_ref={start_ref}
while read -r _old new ref; do
  case "$ref" in
    "$branch")
      case "$new" in
        *[!0]*)
          if [ -z "$start_ref" ] && ! git merge-base --is-ancestor "$start" "$new"; then
            printf 'reference-transaction: %s must descend from %s\n' \
              "$branch" "$start" >&2
            exit 1
          fi
          ;;
      esac
      ;;
    refs/heads/*)
      printf 'reference-transaction: %s may only update %s\n' "$ref" "$branch" >&2
      exit 1
      ;;
  esac
done
"""
PREK_STAGES = ("pre-commit", "pre-push", *(name for name, _ in IDENTITY_HOOKS))
REQUIRED_HOOKS = (
    "reference-transaction",
    *PREK_STAGES,
    *(f"{name}.prek" for name in PREK_STAGES if name != "pre-commit"),
)
HOOKS_PROBE_TIMEOUT = 30


def install_hooks(
    workspace: Path, run_name: str, start: str, *, start_ref: str | None = None
) -> None:
    hooks = workspace / ".git" / "hooks"
    hooks.mkdir(mode=0o700, exist_ok=True)
    hooks.chmod(hooks.stat().st_mode | stat.S_IWUSR)
    prek = shlex.quote(host_tools.resolve("prek"))
    scripts: dict[Path, str] = {}
    for name in PREK_STAGES:
        hook = hooks / (name if name == "pre-commit" else f"{name}.prek")
        scripts[hook] = RUN_PREK_SHIM.format(prek=prek, stage=name)
    for name, verb in IDENTITY_HOOKS:
        scripts[hooks / name] = RUN_IDENTITY_SHIM.format(
            mark=SHIM_MARK, verb=verb, hook=name, run_env=RUN_HOOK_ENV
        )
    run_logs = runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs"
    scripts[hooks / "pre-push"] = pre_push_shim(
        Path(sys.base_prefix, "bin", "python3"),
        TBHB_DEV_ORIGIN.pattern,
        {
            pre_push_guard.RUN_NAME_ENV: run_name,
            pre_push_guard.RUN_START_REF_ENV: start_ref or "",
            pre_push_guard.RUN_START_TIP_ENV: start,
            preflight_gate.RUN_LOG_ENV: str(run_logs / f"{run_name}.log"),
        },
    )
    scripts[hooks / "reference-transaction"] = RUN_REFERENCE_TRANSACTION.format(
        branch=shlex.quote(f"refs/heads/agent/{run_name}"),
        start=shlex.quote(start),
        start_ref=shlex.quote(start_ref or ""),
    )
    for hook, content in scripts.items():
        if hook.exists():
            hook.chmod(hook.stat().st_mode | stat.S_IWUSR)
        hook.write_text(content)
    for entry in (*hooks.rglob("*"), hooks):
        executable = entry.is_dir() or entry in scripts or entry.stat().st_mode & 0o111
        entry.chmod(0o555 if executable else 0o444)


def verify_hook_files(workspace: Path) -> None:
    hooks = workspace / ".git" / "hooks"
    for name in REQUIRED_HOOKS:
        hook = hooks / name
        if not hook.is_file() or not os.access(hook, os.X_OK):
            message = f"run hook {name} is missing or not executable"
            raise Invalid(message)


def verify_run_hooks(workspace: Path, env: Mapping[str, str]) -> None:
    """Check Git's effective hooks directory and the CLI without making a commit."""
    hooks = workspace / ".git" / "hooks"
    probe = partial(
        subprocess.check_output,
        cwd=workspace,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
        timeout=HOOKS_PROBE_TIMEOUT,
    )
    actual = probe(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"]
    ).strip()
    if Path(actual).resolve() != hooks.resolve():
        message = "effective core.hooksPath does not select the run's hooks"
        raise Invalid(message)
    try:
        probe(["tbhbdev", "alpha", "identity", "trailer", "--dry-run"])
    except subprocess.CalledProcessError:
        message = (
            "tbhbdev alpha identity trailer --dry-run failed in the run environment"
        )
        raise Invalid(message) from None


def prepare_hooks(
    workspace: Path,
    name: str,
    start: str,
    env: Mapping[str, str] | None,
    start_ref: str | None,
) -> None:
    with worktree_errors.preparation_step("hook install"):
        install_hooks(workspace, name, start, start_ref=start_ref)
    if env is not None:
        with worktree_errors.preparation_step("hook verification"):
            verify_hook_files(workspace)


def validate_hooks(workspace: Path, writes: tuple[str, ...]) -> None:
    """Read every Git config scope with the host environment, before launch."""
    if not (workspace / ".git").is_dir():
        return
    # The final command override disables hooks for this host-side query. Drop
    # just that value from the result, retaining earlier command-scope values.
    values = subprocess.check_output(  # nosec B603
        [
            host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
            "config",
            "--path",
            "--null",
            "--get-all",
            "core.hooksPath",
        ],
        cwd=workspace,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    ).split("\0")[:-2]
    allowed = {(workspace / ".git/hooks").resolve(), Path("/dev/null")}
    for value in values:
        path = (workspace / value).resolve()
        if path in allowed:
            continue
        writable = next(
            (p for p in writes if path.is_relative_to(Path(p).resolve())), None
        )
        reason = "" if writable is None else f" under write root {writable}"
        message = f"core.hooksPath {path}{reason} is not the workspace hooks directory"
        raise Invalid(message)
