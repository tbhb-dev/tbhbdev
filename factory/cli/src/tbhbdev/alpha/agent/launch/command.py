"""The driver entry point for a bundled agent launch."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from importlib import metadata as pkg
from pathlib import Path

from tbhbdev import userconfig
from tbhbdev.alpha import worker_identity
from tbhbdev.alpha.agent import cli_bundle, settings, transport
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.driver import arguments, metadata, reviews, startup
from tbhbdev.alpha.agent.driver import launch as driver_launch
from tbhbdev.alpha.agent.driver.decisions import launch as plan
from tbhbdev.alpha.agent.driver.decisions import prompt as brief
from tbhbdev.alpha.agent.driver.decisions.output import sandbox
from tbhbdev.alpha.agent.launch import ship
from tbhbdev.alpha.agent.launch.decisions import bootstrap
from tbhbdev.alpha.agent.model import Profile

_OPTIONAL = ("--ref", "--start-ref", "--reason", "--claude-profile")
_EXTRA = ("--unsandboxed", "--schema", "--variant", "--role")
_REFUSALS = (userconfig.ConfigError, OSError, ValueError, pkg.PackageNotFoundError)


def _parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog)
    for flag in ("--host", "--repo", "--model", "--effort"):
        p.add_argument(flag, required=True)
    for flag in (*_OPTIONAL, *_EXTRA):
        p.add_argument(flag)
    for flag in ("--allow-non-gemini", "--ignore-limits", "--steer"):
        p.add_argument(flag, action="store_true")
    p.add_argument("--profile", choices=("build", "review"), required=True)
    p.add_argument("--harness", choices=("codex", "claude", "agy"), default="codex")
    p.add_argument("--fetch-ref", action="append", default=[])
    p.add_argument("--expect-commit", action=argparse.BooleanOptionalAction)
    accounts = p.add_mutually_exclusive_group()
    accounts.add_argument("--account")
    accounts.add_argument("--ops", dest="account", action="store_const", const="ops")
    p.add_argument("--timeout", type=arguments.duration)
    p.add_argument("--class", dest="class_")
    p.add_argument("--worker-workflow", type=worker_identity.workflow)
    p.add_argument("name")
    p.add_argument("prompt_file")
    return p


def _execute(args: argparse.Namespace) -> int:
    loaded = settings.load()
    plan.check_steer(args)
    run = driver_launch.prepare(loaded, args)
    review = run.request.profile is Profile.REVIEW
    if review:
        run.request = replace(run.request, commit=plan.resolve_ref(args.ref, ""))
    prompt_text = Path(args.prompt_file).read_text()
    for warning in brief.check_brief(prompt_text, args):
        print(f"warning: {args.prompt_file}: {warning}", file=sys.stderr)
    schema_text = None if args.schema is None else Path(args.schema).read_text()
    if schema_text is not None:
        brief.check_schema(schema_text)
    bundle, run.cli_commit, run.cli_sha256 = cli_bundle.installed()
    deadline = time.monotonic() + loaded.launch_wait_seconds
    with metadata.launch_lock():
        if review:
            reviews.refuse_live_duplicate(loaded, run)
        metadata.save(run)
    body = json.dumps(request.to_dict(run.request))
    inputs = transport.Inputs(body, prompt_text, schema_text, run.owner)
    try:
        ship.send(loaded, run.host, run, bundle, inputs, deadline - time.monotonic())
        cmd = bootstrap.start(
            run.name, loaded.sockets_dir, run.host.name, steer=args.steer
        )
        owner = transport.Stdin(run.owner)
        transport.call(
            loaded, run.host, cmd, owner, timeout=deadline - time.monotonic()
        )
    except transport.CollisionError:
        metadata.archive_metadata(metadata.metadata_path(run.name), run.started)
        raise
    except transport.TransportError:
        startup.recover_transfer(loaded, run)
        tail = sandbox(run.request.authorized_by)
        msg = f"{run.name}: launch unconfirmed host={run.host.name}"
        print(f"{msg} phase=unreachable{tail}")
        return 75
    try:
        return startup.poll_launch(loaded, run, deadline)
    except transport.CollisionError:
        metadata.not_owner(run)
    except transport.TransportError:
        return startup.unconfirmed(loaded, run, "unreachable")


def main(argv: list[str], prog: str) -> int:
    try:
        args = _parser(prog).parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return _execute(args)
    except _REFUSALS as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
