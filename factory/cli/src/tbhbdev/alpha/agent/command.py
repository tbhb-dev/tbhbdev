"""Dispatch the agent coordinator and host commands."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from tbhbdev import telemetry, userconfig
from tbhbdev.alpha.agent import (
    admission,
    app_server,
    arguments,
    driver,
    settings,
    ssh_serve,
)
from tbhbdev.alpha.agent.decisions import host_names
from tbhbdev.alpha.agent.run_host import configuration, dispatcher

if TYPE_CHECKING:
    import argparse


def main(argv: list[str], prog: str) -> int:
    host_commands = {
        "run-host": dispatcher.main,
        "send-host": app_server.main,
        "ssh-serve": ssh_serve.main,
        "check-slot": admission.main,
    }
    if argv and argv[0] in host_commands:
        return host_commands[argv[0]](argv[1:], f"{prog} {argv[0]}")
    try:
        args = arguments.parser(prog).parse_args(argv) if argv else None
    except SystemExit as exc:
        return int(exc.code or 0)
    if args is not None and args.verb == "exec":
        return telemetry.command(lambda: dispatch(args, prog))
    return dispatch(args, prog)


def dispatch(args: argparse.Namespace | None, prog: str) -> int:
    if args is not None and args.verb == "send" and args.harness == "claude":
        print(driver.CLAUDE_UNAVAILABLE, file=sys.stderr)
        return 1
    try:
        loaded = settings.load()
        if args is None:
            configuration.check_config(loaded)
            print(f"{prog}: configuration valid")
            return 0
        if args.verb == "exec" and host_names.contains_host_name(
            args.name, settings.known_host_names(loaded)
        ):
            print(f"{prog}: run name contains a host name", file=sys.stderr)
            return 1
        return driver.dispatch(loaded, args)
    except (userconfig.ConfigError, OSError, ValueError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
