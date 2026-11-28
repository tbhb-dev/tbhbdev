"""Replay a build run's commits onto a moved start branch, or publish separately."""

from __future__ import annotations

import subprocess  # nosec B404
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import pre_push_guard as guard
from tbhbdev.alpha.agent import credentials, host_tools, push, rescue
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.run_host import publication_plan
from tbhbdev.alpha.gh_app_runtime import GhAppError

if TYPE_CHECKING:
    from collections.abc import Callable

    from tbhbdev.alpha.agent.run_host.state import Prepared, Run

NO_PUSH_MOVED = 12
FAILURES = (OSError, ValueError, subprocess.SubprocessError, Invalid, GhAppError)
FETCH = ("fetch", "--no-tags", "--no-recurse-submodules", "--")
PUSH = ("push", "--no-follow-tags", "--recurse-submodules=no", "--")
PICK = ("cherry-pick", "--allow-empty", "--keep-redundant-commits")
PAIR, SIGN_TIMEOUT = 2, 15


@dataclass(frozen=True)
class _Ctx:
    run: Run
    d: Path
    url: str
    base: str
    head: str
    socket: str | None


def republish(run: Run, prepared: Prepared) -> int | None:
    """Replay onto a moved start ref and push once, or publish separately."""
    start_ref = run.request.start_ref
    bp, hp = run.files.path("base"), run.files.path("head")
    if start_ref is None or run.files.uncertain or not (bp.exists() and hp.exists()):
        return None
    base, head = bp.read_text().strip(), hp.read_text().strip()
    try:
        host, owner, name = repo.parts(run.request.repo)
        url = f"https://{host}/{owner}/{name}.git"
        creds = run.settings.credentials
        acct = next(c for c in creds if c.account == run.request.account)
        perms = credentials.PUSH_PERMISSIONS
        token = credentials.github_token(run.request, acct, build_permissions=perms)
        wt = Path(prepared.paths.worktree)
        with push.publication(run.files.root, wt, head) as d:
            git = partial(push.remote_git, d, token=token)
            ctx = _Ctx(run, d, url, base, head, prepared.paths.signing_socket)
            return _republish(ctx, start_ref, git)
    except FAILURES:
        return _kept(run, "could not publish")


def _kept(run: Run, reason: str) -> None:
    run.files.write("log", f"republish: kept local: {reason}\n")


def _push_checked(
    ctx: _Ctx,
    git: Callable[..., str],
    ref: str,
    span: tuple[str, str],
    start_ref: str | None,
) -> bool:
    base, tip = span
    name = ctx.run.files.name
    if problem := guard.destination_problem(
        ref, deleting=False, agent_push=True, run_name=name, start_ref=start_ref
    ):
        _kept(ctx.run, problem)
        return False
    if problem := rescue.scan(ctx.d, base, tip):
        _kept(ctx.run, problem)
        return False
    if guard.agent_commit_problems(ref, tip, [base], guard.SigningKeys(), ctx.d):
        _kept(ctx.run, "agent commit signature refused")
        return False
    git(*PUSH, ctx.url, f"{tip}:{ref}")
    return True


def _republish(ctx: _Ctx, start_ref: str, git: Callable[..., str]) -> int | None:
    tip, _, ref = git("ls-remote", "--refs", ctx.url, start_ref).partition("\t")
    if ref != start_ref:
        return _kept(ctx.run, "start ref missing")
    if tip == ctx.base:
        return None
    git(*FETCH, ctx.url, f"{start_ref}:refs/republish/start")
    fetched = push.history_git(ctx.d, "rev-parse", "refs/republish/start").strip()
    replayed = _replay(ctx, fetched)
    keys = guard.SigningKeys()
    if replayed is None or guard.agent_commit_problems(
        start_ref, replayed, [fetched], keys, ctx.d
    ):
        return _publish_separate(ctx, git)
    if not _push_checked(ctx, git, start_ref, (fetched, replayed), start_ref):
        return None
    branch = publication_plan.branch_name(ctx.run.files.name, start_ref)
    ctx.run.files.write("head", replayed)
    ctx.run.files.write("branch", branch)
    ctx.run.files.write("log", f"republish: replayed onto {fetched[:12]} and pushed\n")
    return 0


def _publish_separate(ctx: _Ctx, git: Callable[..., str]) -> int | None:
    branch = publication_plan.branch_name(ctx.run.files.name, None)
    ref = f"refs/heads/{branch}"
    # A start-ref run may normally push only start_ref; pass start_ref=None here
    # so the launcher can publish unmodified commits to the separate run branch
    # without loosening pre_push_guard for the agent's own pushes.
    if not _push_checked(ctx, git, ref, (ctx.base, ctx.head), None):
        return None
    ctx.run.files.write("branch", branch)
    ctx.run.files.write("log", f"republish: conflict, published to {branch}\n")
    return NO_PUSH_MOVED


def _replay(ctx: _Ctx, onto: str) -> str | None:
    hgit = partial(push.history_git, ctx.d)
    raw = hgit("rev-list", "--reverse", "--parents", f"{ctx.base}..{ctx.head}")
    rows = [row.split() for row in raw.splitlines()]
    if not rows or any(len(p) != PAIR for p in rows) or rows[0][1] != ctx.base:
        return None
    sign_args, sign_env = _signing_config(ctx.socket)
    with tempfile.TemporaryDirectory(dir=ctx.run.files.root, prefix="rp-") as tmp:
        wt = Path(tmp) / "wt"
        wt.mkdir()
        loc = ("-c", "core.bare=false", f"--git-dir={ctx.d}", f"--work-tree={wt}")
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), **sign_env}
        hgit(*loc, "checkout", "--force", "--detach", onto, "--", extra_env=env)
        for sha, _ in rows:
            raw = hgit("show", "-s", "--format=%cn%n%ce", sha).strip()
            name, _, email = raw.partition("\n")
            who = {"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
            try:
                hgit(*loc, *sign_args, *PICK, sha, extra_env={**env, **who})
            except subprocess.CalledProcessError:
                with suppress(subprocess.SubprocessError):
                    hgit(*loc, "cherry-pick", "--abort", extra_env=env)
                return None
        return hgit("rev-parse", "HEAD").strip()


def _signing_config(socket: str | None) -> tuple[tuple[str, ...], dict[str, str]]:
    try:
        out = subprocess.check_output(  # nosec B603
            [host_tools.resolve("ssh-add"), "-L"],
            env={"SSH_AUTH_SOCK": socket or ""},
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=SIGN_TIMEOUT,
        )
    except OSError, subprocess.SubprocessError:
        return (("-c", "commit.gpgsign=false"), {})
    key = out.splitlines()[0].strip()
    cfg = ("gpg.format=ssh", f"user.signingkey=key::{key}", "commit.gpgsign=true")
    return tuple(x for item in cfg for x in ("-c", item)), {
        "SSH_AUTH_SOCK": socket or ""
    }
