"""Prepare the private workspace and its harness configuration."""

from __future__ import annotations

import json
from pathlib import Path

from tbhbdev.alpha.agent import credentials, push, runfiles, worktree
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Harness, Profile, RunPaths
from tbhbdev.alpha.agent.run_host import bazel_cache, environment, preflight, telemetry
from tbhbdev.alpha.agent.run_host.paths import planned_paths
from tbhbdev.alpha.agent.run_host.state import Prepared, Run
from tbhbdev.alpha.install import hooks, trust


def prepare_workspace(run: Run) -> Prepared:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    paths = planned_paths(run)
    value, _ = preflight.resolve_harness(run, paths)
    workspace = worktree.create(
        clone,
        run.files.name,
        run.request,
        lambda: credentials.fetch_token(run.request, run.settings),
        hook_env=environment.hook_environment(value),
    )
    with worktree.preparation_step("credential delivery"):
        delivery = credentials.deliver(run.request, run.settings, run.tmpdir)
    paths = RunPaths(
        home=str(run.home),
        worktree=str(workspace.resolve()),
        tmpdir=str(run.tmpdir),
        codex_home=paths.codex_home,
        audit_dir=environment.resolve(str(run.home), run.settings.sandbox.audit_dir),
        signing_socket=str(run.tmpdir / "ssh.sock")
        if delivery.signing is not None
        else None,
    )
    with worktree.preparation_step("audit directory"):
        runfiles.private_dir(Path(paths.audit_dir))
    before = None
    if run.request.harness is Harness.CODEX:
        with worktree.preparation_step("codex auth copy"):
            runfiles.private_dir(Path(paths.codex_home))
            source = runfiles.private_dir(run.home / ".codex") / "auth.json"
            before = credentials.auth_copy(source, Path(paths.codex_home) / "auth.json")
        with worktree.preparation_step("codex config"):
            prepare_codex_config(Path(paths.codex_home), run.settings.otel_endpoint)
    start = None
    if run.request.profile is Profile.BUILD and not run.request.no_commit:
        with worktree.preparation_step("stop hook baseline"):
            start = push.head(workspace, f"agent/{run.files.name}")
    return Prepared(
        paths,
        delivery,
        before,
        bazel_cache.bazel_bases(run.home, str(workspace)),
        start,
    )


def prepare_codex_config(home: Path, endpoint: str | None) -> None:
    text = telemetry.otel_config(endpoint) if endpoint is not None else ""
    path = home / "hooks.json"
    config = {"hooks": hooks.stop_hooks("codex")}
    runfiles.atomic(path, json.dumps(config))
    text = trust.trusted_text(text, path, trust.our_hashes(path, config))
    runfiles.atomic(home / "config.toml", text)
