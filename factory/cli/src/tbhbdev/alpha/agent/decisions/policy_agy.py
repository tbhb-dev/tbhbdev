"""Build agy file grants for each run profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions.policy_paths import refuse_overlaps, resolve
from tbhbdev.alpha.agent.model import AgyPolicy, Profile

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import RunPaths, Settings


def write_roots(
    settings: Settings, profile: Profile, paths: RunPaths
) -> tuple[str, ...]:
    if profile is Profile.REVIEW:
        # Reviews only produce audit output, so they need no TMPDIR write grant.
        return (paths.audit_dir,)
    return (
        paths.worktree,
        paths.tmpdir,
        *(resolve(paths.home, p) for p in settings.sandbox.agy.build_write),
    )


def build(
    settings: Settings,
    profile: Profile,
    paths: RunPaths,
    env: dict[str, str],
    deny: tuple[str, ...],
) -> AgyPolicy:
    agy = settings.sandbox.agy
    home = paths.home
    if profile is Profile.REVIEW:
        reads = [resolve(home, p) for p in agy.review_read]
    else:
        reads = [resolve(home, p) for p in agy.build_read]
    writes = write_roots(settings, profile, paths)
    reads += [env[k] for k in ("GH_CONFIG_DIR", "DOCKER_CONFIG") if k in env]
    if paths.signing_socket is not None:
        reads.append(paths.signing_socket)
    # agy has no deny entry, so a grant that reaches a denied path is the leak.
    refuse_overlaps(tuple(reads), deny, "agy grant")
    grants = [
        *(f"read_file({p})" for p in reads),
        *(f"write_file({p})" for p in writes),
    ]
    return AgyPolicy(tuple(grants))
