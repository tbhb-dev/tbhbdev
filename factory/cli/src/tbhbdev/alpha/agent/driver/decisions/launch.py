"""Plan and validate launch requests from the available facts."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import config, request
from tbhbdev.alpha.agent.driver.decisions.launcher import is_reviewer
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
    Profile,
    RunKind,
    RunRequest,
)
from tbhbdev.alpha.identity import models as identity

if TYPE_CHECKING:
    import argparse

    from tbhbdev.alpha.agent.model import Host, Settings


COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def check(
    run: RunRequest,
    settings: Settings,
    host: Host,
    *,
    existing_name: str | None,
    ref: str | None,
) -> RunRequest:
    if existing_name is not None:
        message = (
            "run metadata already exists. "
            f"Run `tbhbdev alpha agent clean --force {existing_name}`"
        )
        raise Invalid(message)
    if ref is not None and run.profile is Profile.BUILD:
        message = (
            "--ref applies only to a review run. "
            "For a build run use --fetch-ref REF --start-ref REF"
        )
        raise Invalid(message)
    if not ref and run.profile is Profile.REVIEW:
        message = "a review run needs --ref with the full commit hash to review"
        raise Invalid(message)
    if run.harness is Harness.CLAUDE:
        name = (
            settings.claude.default_profile
            if run.claude_profile is None
            else run.claude_profile
        )
        profile = config.find_profile(settings.claude.profiles, name)
        if profile is None:
            message = f"claude profile {name!r} isn't defined"
            raise Invalid(message)
        run = replace(run, claude_profile=profile.name)
    request.validate(run, host_allows_unsandboxed=host.unsandboxed)
    return run


def same_version(local: str, remote: str) -> None:
    if local != remote:
        message = f"version mismatch: driver {local!r}; host {remote!r}"
        raise Invalid(message)


def check_retry(local: str, installed: str, original: str) -> None:
    try:
        same_version(local, installed)
    except Invalid as exc:
        message = f"{original} (after one sync)"
        raise Invalid(message) from exc


def resolve_ref(ref: str, text: str) -> str:
    if COMMIT.fullmatch(ref):
        return ref
    lines = [line.split() for line in text.splitlines()]
    # Prefer a peeled tag; a branch and tag may name the same commit.
    records = {name: sha for sha, name in lines}
    commits = {sha for name, sha in records.items() if name + "^{}" not in records}
    if len(commits) != 1 or not COMMIT.fullmatch(next(iter(commits))):
        message = f"ref {ref!r} doesn't resolve to one commit"
        raise Invalid(message)
    return commits.pop()


def check_model(harness: Harness, model: str, *, allow_non_gemini: bool) -> None:
    if (
        harness is Harness.AGY
        and not model.startswith("gemini-")
        and not allow_non_gemini
    ):
        available = "\n  ".join(identity.AGY_MODELS)
        message = (
            "agy requires a gemini-* model or --allow-non-gemini\n"
            f"Models reported by agy\n  {available}"
        )
        raise Invalid(message)


def agy_timeout(settings: Settings, harness: Harness, profile: Profile) -> int | None:
    if harness is not Harness.AGY:
        return None
    if profile is Profile.BUILD:
        return settings.agy_build_timeout_seconds
    return settings.agy_timeout_seconds


def check_steer(args: argparse.Namespace) -> None:
    if args.steer and (args.harness, args.profile, args.schema) != (
        Harness.CODEX,
        Profile.BUILD,
        None,
    ):
        message = "exec --steer requires a Codex build without --schema"
        raise Invalid(message)


def check_options(args: argparse.Namespace, profile: Profile) -> bool:
    if profile is Profile.REVIEW and args.expect_commit is not None:
        message = "request expect_commit applies only to a build run"
        raise Invalid(message)
    reviewer = is_reviewer(args.role, profile)
    if args.worker_workflow and not reviewer:
        message = "--worker-workflow applies to reviewer-role runs only"
        raise Invalid(message)
    return reviewer


def launch_request(
    settings: Settings, args: argparse.Namespace, host: Host
) -> RunRequest:
    harness = Harness(args.harness)
    profile = Profile(args.profile)
    expect_commit = args.expect_commit
    check_model(harness, args.model, allow_non_gemini=args.allow_non_gemini)
    # A review's final commit is read remotely after all local checks pass.
    commit = "0" * 40 if profile is Profile.REVIEW else None
    run = RunRequest(
        kind=RunKind.EXEC,
        repo=args.repo,
        commit=commit,
        harness=harness,
        model=args.model,
        effort=args.effort,
        profile=profile,
        claude_profile=args.claude_profile,
        reason=args.reason,
        account=host.account,
        timeout=args.timeout
        if args.timeout is not None
        else agy_timeout(settings, harness, profile),
        class_=args.class_,
        variant=args.variant,
        role=args.role,
        unsandboxed=True,
        authorized_by=args.unsandboxed,
        startup_deadline_seconds=settings.launch_wait_seconds,
        fetch_refs=tuple(args.fetch_ref),
        start_ref=args.start_ref,
        max_concurrent_runs=host.max_concurrent_runs,
        expect_commit=profile is Profile.BUILD
        if expect_commit is None
        else expect_commit,
        no_commit=expect_commit is False,
    )
    request.check_start_ref(run)
    return run
