"""Route a coordinator command to the recorded owner and target."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev import telemetry
from tbhbdev.alpha.agent import signing_parity, transport
from tbhbdev.alpha.agent.driver import metadata
from tbhbdev.alpha.agent.driver.cleanup import clean, clean_archived, keep
from tbhbdev.alpha.agent.driver.inspection import export_patch, inspect
from tbhbdev.alpha.agent.driver.launch import execute
from tbhbdev.alpha.agent.driver.lifecycle import kill, status, wait
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
    except FileNotFoundError:
        match args.verb:
            case "clean":
                return clean_archived(settings, args.name, force=args.force)
            case "send":
                message = f"{args.name}: unknown run"
                raise Invalid(message) from None
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
        elif args.verb == "send":
            result = send(settings, run, args.message)
        else:
            result = verbs[args.verb](settings, run)
    except transport.CollisionError:
        not_owner(run)
    else:
        return result
