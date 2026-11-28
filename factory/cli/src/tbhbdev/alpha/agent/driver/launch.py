"""Prepare a launch and transfer it to the selected host."""

from __future__ import annotations

import json
import secrets
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tbhbdev import telemetry, version
from tbhbdev.alpha.agent import cli_bundle, transport
from tbhbdev.alpha.agent.decisions import remote_host, remote_run, request
from tbhbdev.alpha.agent.driver import launcher, startup, version_heal
from tbhbdev.alpha.agent.driver.decisions import launch as plan
from tbhbdev.alpha.agent.driver.decisions.hosts import select_host
from tbhbdev.alpha.agent.driver.decisions.launcher import check as check_launcher
from tbhbdev.alpha.agent.driver.decisions.metadata import Run, check_name
from tbhbdev.alpha.agent.driver.decisions.output import sandbox
from tbhbdev.alpha.agent.driver.hosts import launch_host
from tbhbdev.alpha.agent.driver.inputs import checked_prompt, checked_schema
from tbhbdev.alpha.agent.driver.metadata import (
    archive_metadata,
    launch_lock,
    metadata_path,
    not_owner,
    save,
)
from tbhbdev.alpha.agent.driver.reviews import refuse_live_duplicate
from tbhbdev.alpha.agent.model import Profile

if TYPE_CHECKING:
    import argparse

    from tbhbdev.alpha.agent.model import Host, Settings


def prepare(
    settings: Settings, args: argparse.Namespace, *, host: Host | None = None
) -> Run:
    check_name(args.name)
    if host is None:
        host = select_host(settings, args.host, args.account)
    reviewer = plan.check_options(args, Profile(args.profile))
    launched_by = launcher.session(args.worker_workflow, settings.worker_path)
    if reviewer:
        check_launcher(launched_by[0])
    run = plan.launch_request(settings, args, host)
    run = plan.check(
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


def launch_attributes(run: Run) -> None:
    telemetry.attributes(
        **{
            "tbhb.agent.host": run.host.name,
            "tbhb.agent.harness": run.request.harness.value,
            "tbhb.agent.model": run.request.model,
            "tbhb.agent.profile": run.request.profile.value,
            "tbhb.run.id": run.name,
        }
    )


def execute(settings: Settings, args: argparse.Namespace) -> int:
    plan.check_steer(args)
    run = prepare(settings, args) if args.host is not None else None
    if run is not None:
        launch_attributes(run)
    prompt = checked_prompt(args)
    schema = checked_schema(args)
    deadline = time.monotonic() + settings.launch_wait_seconds
    host = launch_host(settings, args, run, deadline)
    if run is None:
        run = prepare(settings, args, host=host)
        launch_attributes(run)
    ref = args.ref or "HEAD"
    resolve = (
        run.request.profile is Profile.REVIEW and plan.COMMIT.fullmatch(ref) is None
    )
    local_version = version.line()
    script = (
        remote_host.preflight(run.request.repo, ref, local_version, run.host)
        if resolve
        else remote_host.installed(run.host)
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
        plan.check_retry(local_version, host_version, mismatch)
    if run.request.profile is Profile.REVIEW:
        run.request = replace(run.request, commit=plan.resolve_ref(ref, refs))
    with launch_lock():
        if run.request.profile is Profile.REVIEW:
            refuse_live_duplicate(settings, run)
        with suppress(Exception):
            bundle, run.cli_commit, run.cli_sha256 = cli_bundle.installed()
            del bundle
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
            remote_run.start(
                run.name, settings.sockets_dir, run.host, steer=args.steer
            ),
            transport.Stdin(run.owner),
            timeout=deadline - time.monotonic(),
        )
    except transport.CollisionError:
        archive_metadata(metadata_path(run.name), run.started)
        raise
    except transport.TransportError:
        startup.recover_transfer(settings, run)
        print(
            f"{run.name}: launch unconfirmed host={run.host.name} phase=unreachable"
            + sandbox(run.request.authorized_by)
        )
        return 75
    try:
        return startup.poll_launch(settings, run, deadline)
    except transport.CollisionError:
        not_owner(run)
    except transport.TransportError:
        return startup.unconfirmed(settings, run, "unreachable")
