"""Route a coordinator command to the recorded owner and target."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev import telemetry
from tbhbdev.alpha.agent import runfiles, signing_parity, transport
from tbhbdev.alpha.agent.driver import lifecycle as life
from tbhbdev.alpha.agent.driver import metadata
from tbhbdev.alpha.agent.driver.cleanup import clean, clean_archived, keep
from tbhbdev.alpha.agent.driver.inspection import export_patch, inspect
from tbhbdev.alpha.agent.driver.launch import execute
from tbhbdev.alpha.agent.driver.metadata import not_owner
from tbhbdev.alpha.agent.driver.steering import send
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    import argparse

    from tbhbdev.alpha.agent.model import Settings


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
        run = metadata.load(settings, args.name)
    except FileNotFoundError as missing:
        return dispatch_without_record(settings, args, missing)
    verbs = {"wait": life.wait, "status": life.status, "kill": life.kill, "keep": keep}
    try:
        if args.verb == "diff" and args.patch:
            result = export_patch(settings, run)
        elif args.verb in {"log", "diff"}:
            limit = args.n if args.verb == "log" else None
            result = inspect(settings, run, limit=limit)
        elif args.verb == "clean":
            print(clean(settings, run, force=args.force), end="")
            result = 0
        elif args.verb == "send":
            result = send(settings, run, args.message)
        else:
            result = verbs[args.verb](settings, run)
    except transport.CollisionError:
        not_owner(run)
    else:
        return result


def dispatch_without_record(
    settings: Settings, args: argparse.Namespace, missing: FileNotFoundError
) -> int:
    match args.verb:
        case "clean":
            return clean_archived(settings, args.name, force=args.force)
        case "kill" | "status":
            files = runfiles.RunFiles(metadata.directory(), args.name)
            if args.verb == "kill":
                return life.kill_local(files, settings.launch_wait_seconds)
            return life.status_local(files)
        case "send":
            message = f"{args.name}: unknown run"
            raise Invalid(message) from None
        case _:
            raise missing
