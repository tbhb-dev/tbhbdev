"""Pack releases through Connect, send one SSH stream, or receive as root."""

import json
import stat
import subprocess  # nosec B404: argv only, no shell
import sys
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
lazy import argparse

from tbhbops.deploy.config import load_sender, service, text
from tbhbops.deploy.protocol import (
    RefusalError,
    array,
    decode,
    mapping,
    pack,
    request,
    require,
)
from tbhbops.deploy.receive import receive
from tbhbops_cli import connect
from tbhbops_cli.common import CliError

REF_PARTS = 3
QUOTE_SAFE = ""
HOST_PUNCTUATION = ".-"

if TYPE_CHECKING:
    from tbhbops_cli.cli import System


def parser(groups: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    top = groups.add_parser("deploy", help="pack, send, and receive service releases")
    commands = top.add_subparsers(dest="command", required=True)
    receiver = commands.add_parser("receive", help="the root deploy receiver")
    receiver.add_argument("--rollback", metavar="SERVICE")
    make = commands.add_parser("pack", help="write a PAX release stream to stdout")
    make.add_argument("--service-file", type=Path, required=True)
    make.add_argument("--release-dir", type=Path, required=True)
    for command in (
        make,
        commands.add_parser("send", help="stream stdin to deploy@host"),
    ):
        command.add_argument("--service", required=True)
        command.add_argument("--release", required=True)
        command.add_argument("--run-id")
    sender = commands.choices["send"]
    sender.add_argument("--host", required=True)
    sender.add_argument("--identity", type=Path, required=True)
    sender.add_argument("--known-hosts", type=Path, required=True)


def resolve(server: connect.Connect, reference: str) -> bytes:
    parts = reference.removeprefix("op://").split("/")
    require(
        reference.startswith("op://") and len(parts) == REF_PARTS and all(parts),
        "secret reference",
    )
    vault, item, field = parts
    listed = server.call("GET", "/v1/vaults", "listing vaults")
    vaults = mapping(decode(json.dumps({"items": listed}).encode())).get("items")
    require(isinstance(vaults, list), "vault listing")
    vaults = array(vaults)
    matches = [
        mapping(entry)
        for entry in vaults
        if mapping(entry).get("name") == vault or mapping(entry).get("id") == vault
    ]
    require(len(matches) == 1, "secret vault lookup")
    vault_id = urllib.parse.quote(text(matches[0].get("id")), safe=QUOTE_SAFE)
    base = f"/v1/vaults/{vault_id}/items"
    listed = decode(json.dumps(server.call("GET", base, "listing items")).encode())
    require(isinstance(listed, list), "item listing")
    listed = array(listed)
    items = [
        mapping(entry)
        for entry in listed
        if mapping(entry).get("title") == item or mapping(entry).get("id") == item
    ]
    require(len(items) == 1, "secret item lookup")
    item_id = urllib.parse.quote(text(items[0].get("id")), safe=QUOTE_SAFE)
    body = mapping(
        decode(
            json.dumps(server.call("GET", f"{base}/{item_id}", "reading item")).encode()
        )
    )
    fields = body.get("fields")
    require(isinstance(fields, list), "item fields")
    fields = array(fields)
    values = [
        mapping(entry).get("value")
        for entry in fields
        if mapping(entry).get("label") == field or mapping(entry).get("id") == field
    ]
    require(len(values) == 1, "secret field lookup")
    return text(values[0]).encode()


def files(directory: Path) -> dict[str, bytes]:
    require(directory.is_dir() and not directory.is_symlink(), "release directory")
    result: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        require(
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
            "release entry type",
        )
        if stat.S_ISREG(info.st_mode):
            result["release/" + path.relative_to(directory).as_posix()] = (
                path.read_bytes()
            )
    return result


def send(args: argparse.Namespace, out: TextIO) -> int:
    wanted = request(
        f"{args.service} {args.release}" + (f" {args.run_id}" if args.run_id else "")
    )
    host = str(args.host)
    require(
        bool(host)
        and all(
            character.isalnum() or character in HOST_PUNCTUATION for character in host
        )
        and not host.startswith("-"),
        "target hostname",
    )
    remote = f"{wanted.service} {wanted.release}" + (
        f" {wanted.run_id}" if wanted.run_id else ""
    )
    argv = [
        "ssh",
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"UserKnownHostsFile={args.known_hosts}",
        "-i",
        str(args.identity),
        f"deploy@{host}",
        remote,
    ]
    result = subprocess.run(  # noqa: S603  # nosec B603
        argv,
        stdin=sys.stdin.buffer,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    lines = result.stdout.splitlines()
    require(bool(lines), "missing receiver result")
    record = mapping(decode(lines[-1]))
    require(
        record.get("stack") == wanted.service
        and record.get("release") == wanted.release,
        "receiver result identity",
    )
    # Print only protocol metadata, never arbitrary SSH output.
    out.write(
        json.dumps(
            {
                key: record.get(key)
                for key in (
                    "stack",
                    "release",
                    "outcome",
                    "step",
                    "duration_s",
                    "run_id",
                )
            }
        )
        + "\n"
    )
    return 0 if result.returncode == 0 and record.get("outcome") == "ok" else 1


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    try:
        if args.command == "receive":
            return receive(out, rollback=args.rollback)
        if args.command == "send":
            return send(args, out)
        wanted = request(
            f"{args.service} {args.release}"
            + (f" {args.run_id}" if args.run_id else "")
        )
        allowed = service(load_sender(args.service_file), wanted.service)
        host = connect.host_url(system.env.get("OP_CONNECT_HOST"), system.run)
        token = system.env.get("OP_CONNECT_TOKEN") or ""
        require(
            bool(token),
            # Only the fixed CliError message is emitted.
            "Connect token required",  # pragma: no mutate
        )
        server = connect.Connect(system.http, host, token)
        secrets = {
            secret.name: resolve(server, secret.op) for secret in allowed.secrets
        }
        body = pack(wanted, files(args.release_dir), secrets)
        require(
            len(body) <= allowed.max_stream_mib * 1024**2,
            # Only the fixed CliError message is emitted.
            "stream limit",  # pragma: no mutate
        )
        sys.stdout.buffer.write(body)
    except RefusalError, OSError, ValueError:
        message = "deploy command refused"
        raise CliError(message) from None
    return 0
