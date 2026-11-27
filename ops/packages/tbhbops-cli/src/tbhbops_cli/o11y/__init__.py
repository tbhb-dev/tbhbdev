"""Observability command group."""

from pathlib import Path
from typing import TYPE_CHECKING, TextIO
lazy import argparse

from tbhbops_cli.o11y import init, receipt, render, smoke, validate
from tbhbops_cli.o11y.filesystem import (
    Filesystem,
    LocalFilesystem,
    effective_gid,
    effective_uid,
)
from tbhbops_cli.o11y.init import execute, gather, generate, owner

if TYPE_CHECKING:
    from tbhbops_cli.cli import System

__all__ = [
    "Filesystem",
    "LocalFilesystem",
    "effective_gid",
    "effective_uid",
    "execute",
    "gather",
    "generate",
    "init",
    "owner",
    "receipt",
    "render",
    "smoke",
    "validate",
]


def add_parser(groups: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = groups.add_parser("o11y", help="prepare the observability stack")
    commands = group.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "init",
        help="prepare host data directories and secrets",
        description="Prepare an existing data directory. Exit 3 means the plan is "
        "incomplete; refusal or I/O errors exit 1, usage errors exit 2.",
    )
    command.add_argument("--data-dir", type=Path, required=True)
    command.add_argument("--secrets-owner", type=owner, default=(0, 0))
    command.add_argument("--dry-run", action="store_true")
    command = commands.add_parser("render", help="build a content-addressed release")
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--config-root", type=render.absolute_path)
    command = commands.add_parser("validate", help="check an observability release")
    command.add_argument("--source", type=Path, required=True)
    command = commands.add_parser("receipt", help="send and query an exact receipt")
    command.add_argument("--otlp-url")
    command.add_argument("--grafana-url", required=True)
    command.add_argument("--signals", default="traces,logs,metrics")
    state = command.add_mutually_exclusive_group()
    state.add_argument("--state-file", type=Path)
    state.add_argument("--recheck", type=Path)
    command.add_argument("--grpc", action="store_true")
    command.add_argument("grpc_argv", nargs=argparse.REMAINDER)
    command = commands.add_parser("smoke", help="exercise a disposable local stack")
    command.add_argument("--dedicated-engine", metavar="ENDPOINT")
    command.add_argument("--stop-after", type=int, choices=[5])
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--artifacts", type=Path, required=True)


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    """Run the selected observability command."""
    if args.command == "receipt":
        return receipt.dispatch(args, system, out)
    if args.command == "smoke":
        return smoke.dispatch(args, system, out)
    if args.command == "render":
        render.dispatch(args, system)
    elif args.command == "validate":
        return validate.dispatch(args, system, out)
    else:
        return init.dispatch(args, system, out)
    return 0
