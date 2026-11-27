"""Read key references and atomically render the private S3 identity file."""

import json
from pathlib import Path
from typing import TYPE_CHECKING
lazy import argparse

from tbhbops import s3_store
from tbhbops_cli.common import CliError

if TYPE_CHECKING:
    from tbhbops_cli.cli import System


def add_parser(groups: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = groups.add_parser("s3", help="prepare the production object store")
    commands = group.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "render-identities",
        help="write private SeaweedFS identities; keys never belong in argv",
        description="Key references are env:VARIABLE or file:PATH. Consumer buckets "
        "are comma-separated. All input is validated before the output is replaced.",
    )
    command.add_argument("--out", type=Path, required=True)
    command.add_argument(
        "--admin", nargs=2, required=True, metavar=("ACCESS_REF", "SECRET_REF")
    )
    command.add_argument(
        "--consumer",
        nargs=4,
        action="append",
        default=[],
        metavar=("NAME", "BUCKETS", "ACCESS_REF", "SECRET_REF"),
        help="repeat for each consumer, including o11y",
    )


def _key(reference: str, system: System) -> str:
    kind, _, name = reference.partition(":")
    if not name:
        message = "key references require a nonempty environment name or file path"
        raise CliError(message)
    if kind == "env":
        return system.env.get(name, "")
    if kind == "file":
        return system.filesystem.read_text(Path(name)).rstrip("\r\n")
    message = "key references must use env:VARIABLE or file:PATH"
    raise CliError(message)


def dispatch(args: argparse.Namespace, system: System) -> int:
    try:
        admin = s3_store.Keys(_key(args.admin[0], system), _key(args.admin[1], system))
        consumers = tuple(
            s3_store.Consumer(
                name,
                tuple(buckets.split(",")),
                s3_store.Keys(_key(access, system), _key(secret, system)),
            )
            for name, buckets, access, secret in args.consumer
        )
        document = s3_store.build_identities(admin, consumers)
        system.filesystem.write_atomic(args.out, json.dumps(document) + "\n", 0o600)
    except OSError, ValueError:
        # Neither paths nor exception messages are safe: they may contain keys.
        message = "identity rendering failed; check inputs and output permissions"
        raise CliError(message) from None
    return 0
