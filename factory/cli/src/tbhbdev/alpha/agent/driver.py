"""The coordinating host: gather facts, ask rules, execute bounded actions."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NoReturn

from tbhbdev import telemetry, version
from tbhbdev.alpha import brief
from tbhbdev.alpha.agent import (
    launcher,
    render,
    runfiles,
    shims,
    signing_parity,
    transport,
    version_heal,
)
from tbhbdev.alpha.agent.decisions import driver as rules
from tbhbdev.alpha.agent.decisions import launcher as launch_rules
from tbhbdev.alpha.agent.decisions import limits, remote, repo, request
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile, RunKind, RunRequest

if TYPE_CHECKING:
    import argparse
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import Host, Settings


MAX_LIVE_REVIEWS = 4


class Run:
    """The recorded target and request, with no settings or credential references."""

    def __init__(
        self,
        name: str,
        host: Host,
        run: RunRequest,
        started: str,
        owner: str,
    ) -> None:
        self.name = name
        self.host = host
        self.request = run
        self.started = started
        self.owner = owner
        self.launched_by: tuple[str | None, str | None] = (None, None)
        self.cleaning = False
        self.ignore_limits = False


def directory() -> Path:
    return runfiles.state_home(Path.home()) / files.DIRECTORY


def metadata_path(name: str) -> Path:
    rules.check_name(name)
    return directory() / f"{name}.json"


def save(run: Run) -> None:
    path = metadata_path(run.name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    record = {
        **request.to_dict(run.request),
        "host": run.host.name,
        "started": run.started,
        "owner": run.owner,
        "launcher": run.launched_by[0],
        "launcher_session": run.launched_by[1],
        "cleaning": run.cleaning,
        "ignore_limits": run.ignore_limits,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(record, output)
        output.write("\n")


def _maybe_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def load(settings: Settings, name: str, *, archived: Path | None = None) -> Run:
    path = metadata_path(name) if archived is None else archived
    if archived is None:
        path.parent.chmod(0o700)
    try:
        record: dict[str, object] = json.loads(path.read_text())
        host = rules.select_host(
            settings, str(record.pop("host")), str(record["account"])
        )
        started = str(record.pop("started"))
        owner = str(record.pop("owner"))
        cleaning = record.pop("cleaning")
        launched_by = (
            _maybe_text(record.pop("launcher", None)),
            _maybe_text(record.pop("launcher_session", None)),
        )
        # Records from before the limits check have no such field.
        ignore_limits = record.pop("ignore_limits", False)
        record.pop("tainted_host", None)
        run = request.parse(record, host_allows_unsandboxed=True)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        message = f"{path}: invalid run metadata"
        raise Invalid(message) from exc
    result = Run(name, host, run, started, owner)
    result.launched_by = launched_by
    result.cleaning = bool(cleaning)
    result.ignore_limits = bool(ignore_limits)
    return result


def agy_timeout(settings: Settings, harness: Harness, profile: Profile) -> int | None:
    if harness is not Harness.AGY:
        return None
    if profile is Profile.BUILD:
        return settings.agy_build_timeout_seconds
    return settings.agy_timeout_seconds


def prepare(settings: Settings, args: argparse.Namespace) -> Run:
    rules.check_name(args.name)
    host = rules.select_host(settings, args.host, args.account)
    harness = Harness(args.harness)
    profile = Profile(args.profile)
    expect_commit = args.expect_commit
    if profile is Profile.REVIEW and expect_commit is not None:
        message = "request expect_commit applies only to a build run"
        raise Invalid(message)
    reviewer = launch_rules.is_reviewer(args.role, profile)
    if args.worker_workflow and not reviewer:
        message = "--worker-workflow applies to reviewer-role runs only"
        raise Invalid(message)
    launched_by = launcher.session(args.worker_workflow, settings.worker_path)
    if reviewer:
        launch_rules.check(launched_by[0])
    rules.check_model(harness, args.model, allow_non_gemini=args.allow_non_gemini)
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
    run = rules.check(
        run,
        settings,
        host,
        existing_name=args.name if metadata_path(args.name).exists() else None,
        ref=args.ref,
    )
    result = Run(
        args.name,
        host,
        run,
        datetime.now(UTC).isoformat(),
        secrets.token_hex(16),
    )
    result.launched_by = launched_by
    result.ignore_limits = args.ignore_limits
    return result


# A recorded run with no known time limit counts as live this long.
UNKNOWN_LIMIT_SECONDS = 6 * 60 * 60


def past_time_limit(settings: Settings, run: Run) -> bool:
    """Whether RUN, recorded at launch, can no longer be running.

    The limit is the shortest of the request's timeout and the host run cap, or
    six hours when neither is known. The launch wait and the kill grace add to
    it, because the clock starts at preparation and the run starts later.
    """
    limits = [run.request.timeout]
    if settings.max_run_minutes is not None:
        limits.append(settings.max_run_minutes * 60)
    seconds = min((n for n in limits if n is not None), default=UNKNOWN_LIMIT_SECONDS)
    seconds += settings.launch_wait_seconds + settings.kill_grace_seconds
    age = datetime.now(UTC) - datetime.fromisoformat(run.started)
    return age.total_seconds() > seconds


def canonical_repo(repo: str) -> str:
    """Lowercase owner/name, without a github.com prefix or a .git suffix."""
    name = repo.strip().lower()
    for prefix in ("https://github.com/", "github.com/"):
        name = name.removeprefix(prefix)
    return name.removesuffix(".git")


@contextmanager
def launch_lock() -> Generator[None]:
    """Hold the coordinator-side lock that orders duplicate checks and saves."""
    folder = directory()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    descriptor = os.open(
        folder / "launch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def refuse_live_duplicate(settings: Settings, run: Run) -> None:
    """Refuse a duplicate review or a fifth live review of the same commit.

    Two runs on one repository, commit and profile coexist only when both
    declare distinct variants. A run with no variant conflicts with every
    live run on that key. The cap applies across variants.

    A recorded run counts as live until the host reports its exit status, so
    an unreachable host or a run with no status also refuses. A run older than
    its time limit no longer counts, because the host stops it by then. A
    record with no time limit stays live for six hours. The caller holds
    ``launch_lock`` so two launches cannot both pass this check.

    Raises:
        Invalid: A live review conflicts on variant or the cap is reached.
    """
    key = (canonical_repo(run.request.repo), run.request.commit, run.request.profile)
    live = 0
    for path in sorted(directory().glob("*.json")):
        try:
            other = load(settings, path.stem)
        except Invalid, ValueError, FileNotFoundError:
            continue
        other_key = (
            canonical_repo(other.request.repo),
            other.request.commit,
            other.request.profile,
        )
        if other_key != key or past_time_limit(settings, other):
            continue
        try:
            ended = (
                transport.read(settings, other.host, (other.name, other.owner)).rc
                is not None
            )
        except transport.TransportError:
            ended = False
        if ended:
            continue
        live += 1
        if live >= MAX_LIVE_REVIEWS:
            message = (
                f"{run.name}: {run.request.repo}@{run.request.commit} already has "
                f"{MAX_LIVE_REVIEWS} live reviews"
            )
            raise Invalid(message)
        variant, other_variant = run.request.variant, other.request.variant
        if None not in (variant, other_variant) and variant != other_variant:
            continue
        message = (
            f"{run.name}: run {other.name} already reviews "
            f"{run.request.repo}@{run.request.commit} with the "
            f"{run.request.profile.value} profile and hasn't ended. "
            f"Check `tbhbdev alpha agent status {other.name}` and use "
            f"`tbhbdev alpha agent clean {other.name}` once it ends. "
            "For concurrent reviewers, launch both with distinct --variant values"
        )
        raise Invalid(message)


def check_limits(
    settings: Settings,
    host: Host,
    profile: Profile,
    *,
    ignore: bool,
    timeout: float = 30,
) -> None:
    """Read HOST's load, memory, pressure and swap in one ssh call, then refuse or warn.

    Launch calls this first. A later step may call it again.

    Raises:
        Invalid: A limit is exceeded or the read failed, and neither the host
            nor IGNORE allows the launch to continue.
    """
    if host.max_concurrent_runs == 0:
        message = f"{host.name}: max_concurrent_runs is 0"
        raise Invalid(message)
    try:
        found = limits.parse(
            transport.shell(settings, host, remote.limits(), timeout=timeout)
        )
    except transport.TransportError as exc:
        found = str(exc)
    memory_mib = (
        settings.review_memory_mib
        if profile is Profile.REVIEW
        else settings.run_memory_mib
    )
    refuse, message = limits.decide(host, found, memory_mib, ignore=ignore)
    if refuse:
        raise Invalid(message)
    if message:
        print(message, file=sys.stderr)


_SECRET_PATTERNS = (
    ("1Password reference", re.compile(r"op://")),
    (
        "private key header",
        re.compile(
            r"-----BEGIN (?:(?:[A-Z0-9 ]+ )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
        ),
    ),
    ("token prefix", re.compile(r"ghp_|github_pat_|sk-ant-|AKIA|xoxb-")),
)


def secret_kind(text: str) -> str | None:
    """Return the kind of the first matching pattern."""
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def checked_prompt(args: argparse.Namespace) -> str:
    """Read the brief, refuse secrets and conflicts, and report warnings locally."""
    prompt = Path(args.prompt_file).read_text()
    if kind := secret_kind(prompt):
        telemetry.attributes(**{"tbhb.agent.outcome": "refused"})
        message = f"brief refused: {kind}"
        raise Invalid(message)
    if not prompt and version_heal.retrying():
        message = f"{args.prompt_file}: brief is empty after the driver restarted"
        raise Invalid(message)
    pinned_refs = (args.ref,) if args.ref and args.ref.startswith("refs/") else ()
    available_refs = [*args.fetch_ref, *pinned_refs]
    problems = brief.check_hooks(prompt)
    problems.extend(
        rules.prompt_launch_problems(
            prompt, Profile(args.profile), available_refs, args.start_ref
        )
    )
    if problems:
        telemetry.attributes(**{"tbhb.agent.outcome": "refused"})
        message = "\n".join(f"{args.prompt_file}: {problem}" for problem in problems)
        raise Invalid(message)
    malformed_refs = rules.malformed_prompt_refs(prompt)
    for warning in brief.branch_warnings(
        prompt,
        run=args.name,
        fetch_refs=tuple(args.fetch_ref),
        ignored_refs=(*pinned_refs, *malformed_refs),
    ):
        print(f"warning: {args.prompt_file}: {warning}", file=sys.stderr)
    for ref in malformed_refs:
        print(
            f"warning: {args.prompt_file}: malformed ref {ref!r} in the prompt",
            file=sys.stderr,
        )
    for ref in rules.unfetched_prompt_refs(prompt, available_refs):
        print(
            f"warning: {args.prompt_file}: the prompt names {ref} but this launch "
            f"has no --fetch-ref {ref}; "
            "a run whose mirror lacks it may end with no work",
            file=sys.stderr,
        )
    return prompt


def execute(settings: Settings, args: argparse.Namespace) -> int:
    run = prepare(settings, args)
    telemetry.attributes(
        **{
            "tbhb.agent.host": run.host.name,
            "tbhb.agent.harness": run.request.harness.value,
            "tbhb.agent.model": run.request.model,
            "tbhb.agent.profile": run.request.profile.value,
            "tbhb.run.id": run.name,
        }
    )
    prompt = checked_prompt(args)
    schema = None if args.schema is None else Path(args.schema).read_text()
    if schema is not None:
        if kind := secret_kind(schema):
            telemetry.attributes(**{"tbhb.agent.outcome": "refused"})
            message = f"schema refused: {kind}"
            raise Invalid(message)
        json.loads(schema)
    deadline = time.monotonic() + settings.launch_wait_seconds
    try:
        check_limits(
            settings,
            run.host,
            run.request.profile,
            ignore=run.ignore_limits,
            timeout=deadline - time.monotonic(),
        )
    except Invalid:
        telemetry.attributes(**{"tbhb.agent.outcome": "refused"})
        raise
    ref = args.ref or "HEAD"
    resolve = (
        run.request.profile is Profile.REVIEW and rules.COMMIT.fullmatch(ref) is None
    )
    local_version = version.line()
    script = (
        remote.preflight(run.request.repo, ref, local_version, run.host)
        if resolve
        else remote.installed(run.host)
    )
    preflight = transport.shell(
        settings, run.host, script, timeout=deadline - time.monotonic()
    )
    host_version, _, refs = preflight.partition("\n")
    mismatch = version_heal.check(settings, run.host, local_version, host_version)
    if mismatch is not None:
        deadline = time.monotonic() + settings.launch_wait_seconds
        preflight = transport.shell(
            settings, run.host, script, timeout=deadline - time.monotonic()
        )
        host_version, _, refs = preflight.partition("\n")
        version_heal.check_retry(local_version, host_version, mismatch)
    if run.request.profile is Profile.REVIEW:
        run.request = replace(run.request, commit=rules.resolve_ref(ref, refs))
    with launch_lock():
        if run.request.profile is Profile.REVIEW:
            refuse_live_duplicate(settings, run)
        save(run)
    try:
        transport.stream(
            settings,
            run.host,
            run.name,
            inputs=transport.Inputs(
                json.dumps(request.to_dict(run.request)), prompt, schema, run.owner
            ),
            timeout=deadline - time.monotonic(),
        )
        transport.call(
            settings,
            run.host,
            remote.start(run.name, settings.sockets_dir, run.host),
            transport.Stdin(run.owner),
            timeout=deadline - time.monotonic(),
        )
    except transport.CollisionError:
        archive_metadata(metadata_path(run.name), run.started)
        raise
    except transport.TransportError:
        recover_transfer(settings, run)
        print(
            f"{run.name}: launch unconfirmed host={run.host.name} phase=unreachable"
            + render.sandbox(run.request.authorized_by)
        )
        return 75
    try:
        return poll_launch(settings, run, deadline)
    except transport.CollisionError:
        not_owner(run)
    except transport.TransportError:
        return unconfirmed(settings, run, "unreachable")


def not_owner(run: Run) -> NoReturn:
    archive_metadata(metadata_path(run.name), datetime.now(UTC).isoformat())
    message = f"{run.name}: not owner"
    raise Invalid(message)


def recover_transfer(settings: Settings, run: Run) -> None:
    try:
        transport.shell(
            settings, run.host, remote.owned(run.name), transport.Stdin(run.owner)
        )
    except transport.CollisionError:
        not_owner(run)
    except transport.TransportError:
        # An unreachable host may have started the run. Keep its owner claim
        # so later verbs can reach it once the connection recovers.
        pass


def poll_launch(settings: Settings, run: Run, deadline: float) -> int:
    poll_deadline = deadline
    while True:
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            timeout=poll_deadline - time.monotonic(),
        )
        poll_deadline = rules.launch_deadline(
            reading.phase, deadline, shims.SHIM_SECONDS
        )
        action = rules.launch(
            reading.rc,
            launched=reading.launched,
            expired=time.monotonic() >= poll_deadline,
        )
        if action == "finished":
            # The host may still be closing its pane after committing the rc.
            transport.shell(
                settings,
                run.host,
                remote.session(run.name, "kill-session", settings.sockets_dir),
                transport.Stdin(run.owner),
            )
            print(
                f"{run.name}: rc={reading.rc}"
                f"{render.sandbox(run.request.authorized_by)}"
                f"\n{reading.text}",
                end="",
            )
            if reading.rc and not reading.launched:
                # A refusal before the harness starts must release both claims
                # so the name can be used again. Keep the refusal log in trash.
                clean(settings, run, force=True)
            return reading.rc or 0
        if action == "launched":
            print(
                render.launched(
                    run.name,
                    run.host.name,
                    run.host.account,
                    authorized_by=run.request.authorized_by,
                )
            )
            return 0
        if action == "deadline":
            return unconfirmed(settings, run, reading.phase)
        time.sleep(0.5)


def unconfirmed(settings: Settings, run: Run, phase: str) -> int:
    print(
        f"{run.name}: launch unconfirmed host={run.host.name} phase={phase}"
        + render.sandbox(run.request.authorized_by)
    )
    try:
        kill(settings, run)
    except transport.CollisionError:
        not_owner(run)
    return 1


def wait(settings: Settings, run: Run) -> int:
    rules.wait_kind(run.request.kind)
    poll = rules.wait_seconds(
        os.environ.get("TBHB_AGENT_WAIT_POLL", "30"), "TBHB_AGENT_WAIT_POLL"
    )
    unreachable = rules.wait_seconds(
        os.environ.get("TBHB_AGENT_WAIT_UNREACHABLE", "240"),
        "TBHB_AGENT_WAIT_UNREACHABLE",
    )
    failed_since = None
    while True:
        attempt = time.monotonic()
        timeout = (
            unreachable
            if failed_since is None
            else unreachable - (attempt - failed_since)
        )
        try:
            reading = transport.read(
                settings,
                run.host,
                (run.name, run.owner),
                answer=True,
                timeout=min(30, timeout),
            )
        except transport.TransportError:
            now = time.monotonic()
            if failed_since is None:
                failed_since = attempt
            result = rules.wait(
                None,
                session=True,
                failed_seconds=now - failed_since,
                unreachable=unreachable,
            )
            if result is not None:
                print(
                    f"{run.name}: rc=unreachable"
                    + render.sandbox(run.request.authorized_by)
                )
                print(render.INSPECTION_WARNING)
                return result
        else:
            failed_since = None
            result = rules.wait(
                reading.rc,
                session=reading.session,
                failed_seconds=0,
                unreachable=unreachable,
            )
            if result is not None:
                rc = rules.answer_state(reading.rc)
                print(
                    f"{run.name}: rc={rc}"
                    f"{render.sandbox(run.request.authorized_by)}\n{reading.text}",
                    end="",
                )
                print("\n" + render.INSPECTION_WARNING)
                settle(settings, run, result)
                return result
        time.sleep(max(0.5, poll))


def settle(settings: Settings, run: Run, result: int) -> None:
    if result == 0 and not run.request.unsandboxed:
        try:
            clean(settings, run, force=False)
        except Invalid as error:
            print(error, file=sys.stderr)
        except Exception as error:  # noqa: BLE001 -- the result is final; a failed clean must not replace it
            print(f"cleanup state unknown: {error}", file=sys.stderr)
        else:
            return
    tree = repo.clone_path(PurePosixPath("."), run.request.repo)
    print(
        f"{run.name}: worktree kept at "
        f"{run.host.name}:~/{tree}/.worktrees/agent-{run.name}"
    )


def status(settings: Settings, run: Run) -> int:
    try:
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            reason=True,
        )
    except transport.TransportError:
        # The host can't answer, so the run may still be live. Print the
        # local record and say the state is unknown instead of failing bare.
        print(
            render.status(
                run.name,
                phase="unknown",
                rc="unknown",
                target=(
                    run.host.name,
                    run.host.account,
                    run.request.authorized_by,
                    run.launched_by[0],
                ),
                started=run.started,
            )
            + " host unreachable, run state unknown"
        )
        return 1
    rc = rules.state(reading.rc, session=reading.session)
    if reading.rc and reading.text:
        rc += f" ({reading.text.rstrip()})"
    print(
        render.status(
            run.name,
            phase=reading.phase,
            rc=rc,
            target=(
                run.host.name,
                run.host.account,
                run.request.authorized_by,
                run.launched_by[0],
            ),
            started=run.started,
        )
    )
    return 0


def kill(settings: Settings, run: Run) -> int:
    transport.shell(
        settings,
        run.host,
        remote.session(run.name, "kill-session", settings.sockets_dir),
        transport.Stdin(run.owner),
    )
    deadline = time.monotonic() + settings.kill_grace_seconds + 2
    while True:
        remaining = deadline - time.monotonic()
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            timeout=min(30, max(1, remaining)),
        )
        action = rules.kill(
            reading.rc, session=reading.session, expired=time.monotonic() >= deadline
        )
        if action == "finished":
            return 0
        if action == "write":
            transport.shell(
                settings,
                run.host,
                remote.fallback(run.name, settings.sockets_dir),
                transport.Stdin(run.owner),
            )
            return 0
        if action == "refuse":
            message = f"{run.name}: session still exists; return code left to run-host"
            raise Invalid(message)
        time.sleep(0.5)


def archive_metadata(path: Path, stamp: str) -> None:
    trash = directory() / "trash" / stamp
    trash.mkdir(mode=0o700, parents=True)
    path.rename(trash / path.name)


def check_clean(
    settings: Settings, run: Run, reading: transport.Reading, *, force: bool
) -> None:
    rules.clean(reading.rc, session=reading.session, force=force or run.cleaning)
    if not force and run.request.unsandboxed:
        message = (
            "full-access run may have unpublished commits or unread output; "
            "pass --force to clean anyway"
        )
        raise Invalid(message)
    if not force:
        branch = transport.shell(
            settings,
            run.host,
            remote.origin_branch(run.request.repo, run.name),
            transport.Stdin(run.owner),
        )
        if not any(
            line.endswith(f"\trefs/heads/agent/{run.name}")
            for line in branch.splitlines()
        ):
            message = (
                f"branch agent/{run.name} does not exist on origin; "
                "pass --force to clean anyway"
            )
            raise Invalid(message)
        print("branch exists on origin")


def clean(
    settings: Settings, run: Run, *, force: bool, archived: Path | None = None
) -> int:
    path = metadata_path(run.name) if archived is None else archived
    # An archived record may only recover a missing claim. The host still
    # refuses an existing owner or a live run.
    ownerless_only = True
    if archived is None:
        try:
            reading = transport.read(settings, run.host, (run.name, run.owner))
        except transport.CollisionError:
            pass
        else:
            check_clean(settings, run, reading, force=force)
            ownerless_only = False
        record = json.loads(path.read_text())
        record["cleaning"] = True
        path.write_text(json.dumps(record) + "\n")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    transport.shell(
        settings,
        run.host,
        remote.clean(
            run.request.repo,
            run.name,
            stamp,
            settings.sockets_dir,
            force=force,
            ownerless_only=ownerless_only,
        ),
        transport.Stdin(run.owner),
    )
    archive_metadata(path, stamp)
    return 0


def clean_archived(settings: Settings, name: str, *, force: bool) -> int:
    archived = max(
        (directory() / "trash").glob(f"*/{name}.json"),
        key=lambda path: path.parent.stat().st_mtime_ns,
        default=None,
    )
    # Completed cleanups use a compact UTC stamp ending in Z. Ownership
    # failures use an ISO stamp ending in +00:00 and may leave host files.
    if archived is None or archived.parent.name.endswith("Z"):
        return 0
    run = load(settings, name, archived=archived)
    return clean(settings, run, force=force, archived=archived)


def export_patch(settings: Settings, run: Run) -> int:
    if run.request.profile is not Profile.BUILD:
        message = "diff --patch requires a finished build run"
        raise Invalid(message)
    script = remote.export_patch(run.request.repo, run.name)
    output = transport.call_bytes(
        settings, run.host, ("/bin/sh", "-c", script), transport.Stdin(run.owner)
    )
    sys.stdout.buffer.write(output)
    return 0


def inspect(settings: Settings, run: Run, *, limit: int | None) -> int:
    if (
        run.request.unsandboxed
        and limit is None
        and run.request.profile is Profile.REVIEW
    ):
        reading = transport.read(
            settings, run.host, (run.name, run.owner), answer=True, timeout=30
        )
        print(transport.terminal_text(reading.text), end="")
        return 0
    script = (
        remote.run_log(run.name)
        if run.request.unsandboxed and limit is not None
        else remote.inspect(run.request.repo, run.name, limit=limit)
    )
    print(
        transport.terminal_text(
            transport.shell(
                settings,
                run.host,
                script,
                transport.Stdin(run.owner),
            )
        ),
        end="",
    )
    return 0


def keep(settings: Settings, run: Run) -> int:
    transport.shell(
        settings, run.host, remote.keep(run.name), transport.Stdin(run.owner)
    )
    return 0


def dispatch(settings: Settings, args: argparse.Namespace) -> int:
    if args.verb == "signing-parity":
        return signing_parity.main(settings)
    if args.verb == "exec":
        result = execute(settings, args)
        telemetry.attributes(
            **{"tbhb.agent.outcome": "ok" if result == 0 else "failed"}
        )
        return result
    try:
        run = load(settings, args.name)
    except FileNotFoundError:
        if args.verb == "clean":
            return clean_archived(settings, args.name, force=args.force)
        raise
    verbs = {"wait": wait, "status": status, "kill": kill, "keep": keep}
    try:
        if args.verb == "diff" and args.patch:
            result = export_patch(settings, run)
        elif args.verb in {"log", "diff"}:
            limit = args.n if args.verb == "log" else None
            result = inspect(settings, run, limit=limit)
        elif args.verb == "clean":
            result = clean(settings, run, force=args.force)
        else:
            result = verbs[args.verb](settings, run)
    except transport.CollisionError:
        not_owner(run)
    else:
        return result
