"""The tbhbops command.

    tbhbops gitleaks generate [--dry-run]
    tbhbops connect token-commands [--host H ...] [--ops-session H]
                                  [--runner-vm H ...] [--dry-run]
    tbhbops connect check [--host H] [--ops-controller] [--inventory PATH]
    tbhbops o11y init --data-dir PATH [--secrets-owner UID:GID] [--dry-run]
    tbhbops o11y render --source PATH --out PATH [--config-root ABSOLUTE_PATH]
    tbhbops o11y validate --source PATH

Run `tbhbops gitleaks generate --help` for the settings it reads.
"""

import argparse
import os
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
lazy from collections.abc import Callable, Mapping

from tbhbops import dns_smoke

from tbhbops_cli import (
    config,
    connect,
    deploy,
    gitleaks,
    inventory,
    o11y,
    s3,
    sources,
    tokens,
)
from tbhbops_cli.common import CliError, Http, Run, https_request
from tbhbops_cli.o11y.http import AdminHttp, LocalAdminHttp, LocalHttp
from tbhbops_cli.o11y.http import Http as ReceiptHttp
from tbhbops_cli.o11y.ports import LocalPorts, Ports
from tbhbops_cli.o11y.runner import Runner, SubprocessRunner

PROG = "tbhbops"

_SETTINGS = "\n".join(f"  {name}\n      {use}" for name, use in config.ENVIRONMENT)
EPILOG = f"""\
Environment (no token is ever printed):
{_SETTINGS}

Until the two tokens exist, @tbhb runs this command under their own session.

The hosts come from the Ansible inventory, ansible/inventory/, found from the
current directory. The owners, the 1Password account and the Connect server
come from the tbhbdev config, $XDG_CONFIG_HOME/tbhbdev/config.toml.
"""


@dataclass(frozen=True)
class System:
    """What the command touches outside its arguments. Tests replace it."""

    env: Mapping[str, str]
    http: Http
    run: Run
    cwd: Path
    words: Path
    now: datetime
    hostname: str
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    egid: int = field(default_factory=o11y.effective_gid)
    euid: int = field(default_factory=o11y.effective_uid)
    filesystem: o11y.Filesystem = field(default_factory=o11y.LocalFilesystem)
    choice: Callable[[str], str] = secrets.choice
    runner: Runner = field(default_factory=SubprocessRunner)
    receipt_http: ReceiptHttp = field(default_factory=LocalHttp)
    admin_http: AdminHttp = field(default_factory=LocalAdminHttp)
    epoch: Callable[[], float] = time.time
    random_bytes: Callable[[int], bytes] = secrets.token_bytes
    platform: str = sys.platform
    engine_hostname: str = field(default_factory=socket.gethostname)
    ports: Ports = field(default_factory=LocalPorts)


def real_system() -> System:
    """This process's environment, network, files, clock and host name."""
    return System(
        os.environ,
        https_request,
        sources.run_command,
        Path.cwd(),
        sources.WORDS,
        datetime.now(UTC),
        socket.gethostname().partition(".")[0].lower(),
    )


def parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    top = argparse.ArgumentParser(prog=PROG, description="The tbhb-dev/tbhbdev tools.")
    groups = top.add_subparsers(dest="group", required=True)
    deploy.parser(groups)
    leaks = groups.add_parser("gitleaks", help="the private gitleaks config")
    commands = leaks.add_subparsers(dest="command", required=True)
    generate = commands.add_parser(
        "generate",
        help="generate the private gitleaks config and write it to 1Password",
        description=(
            f"Generate the private gitleaks config and write it to the {gitleaks.FIELD}"
            f" field of the 1Password item {gitleaks.ITEM} in the {gitleaks.VAULT}"
            " vault."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    generate.add_argument(
        "--dry-run",
        action="store_true",
        help="print what it would write, which never includes the config, and stop",
    )
    link = groups.add_parser("connect", help="the agent hosts' Connect tokens")
    steps = link.add_subparsers(dest="command", required=True)
    make = steps.add_parser(
        "token-commands",
        help="print the shell script @tbhb runs to create the agent hosts' tokens",
        description=(
            "Print, and never run, a POSIX shell script that grants the Connect"
            " server its vaults and creates one token with no expiry for each agent"
            f" host. The server comes from [connect] server in the tbhbdev config,"
            f" and the hosts from the {tokens.AGENTS} group of the inventory."
        ),
    )
    make.add_argument(
        "--host",
        action="append",
        default=[],
        help="make a token for only this agent host; repeat it for more",
    )
    make.add_argument(
        "--ops-session",
        metavar="HOST",
        help=(
            "also make an ops-session token for the operator's agent session on"
            " this operator Mac, and keychain it as the user who runs the script;"
            " off unless given, and it takes a host so the Mac is never guessed"
        ),
    )
    make.add_argument(
        "--runner-vm",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            f"make a {tokens.RUNNER_PREFIX} token for this CI runner VM, which"
            f" reads only the vault {tokens.VAULT_RUNNERS}, and store it in its lab"
            f" node's vault as {tokens.RUNNER_ITEM}; repeat it for more. Without"
            f" --host and --ops-session, the script makes only these tokens; with"
            " either, it makes theirs too"
        ),
    )
    make.add_argument(
        "--dry-run",
        action="store_true",
        help="print the same script with each op command echoed instead of run",
    )
    checks = steps.add_parser(
        "check",
        help="check that this host's token reaches the vaults it should",
        description=(
            "List the vaults the token in OP_CONNECT_TOKEN reaches, and compare"
            " them with the vaults the script grants the host. A vault the token"
            " lacks is missing, and one it shouldn't reach is extra; either one"
            " exits 1."
        ),
    )
    checks.add_argument(
        "--host", help="the agent host to check; default this host's short name"
    )
    checks.add_argument(
        "--ops-controller",
        action="store_true",
        help="check an ops-controller token, which reads exactly the host-ops vaults",
    )
    checks.add_argument(
        "--inventory",
        type=Path,
        help=(
            "the inventory directory, which holds hosts.yml; default the"
            " ops/ansible/inventory of the private companion"
        ),
    )
    dns_group = groups.add_parser("dns", help="the CoreDNS network resolver")
    dns_commands = dns_group.add_subparsers(dest="command", required=True)
    smoke = dns_commands.add_parser(
        "smoke",
        help="start CoreDNS in Docker and check its views and forwarder",
        description=(
            "Start the pinned CoreDNS image with the project's Corefile, zones"
            " and hardening flags inside Docker, and check the LAN view,"
            " tailnet view, refused view and forwarder block."
        ),
    )
    smoke.add_argument(
        "--source",
        type=Path,
        default=Path("apps/dns"),
        help="the DNS Compose project directory; default apps/dns",
    )
    smoke.add_argument(
        "--artifacts",
        type=Path,
        default=Path("dns-smoke-artifacts"),
        help="the directory for failure logs; default dns-smoke-artifacts",
    )
    smoke.add_argument(
        "--dedicated-engine",
        metavar="DOCKER_HOST",
        help=("the dedicated Docker engine URI, such as unix:///run/dind/docker.sock"),
    )
    o11y.add_parser(groups)
    s3.add_parser(groups)
    return top


def _facts(
    system: System,
    creds: config.Credentials,
    settings: config.Settings,
    vaults: dict[str, str],
) -> gitleaks.Facts:
    """What the config keeps out, from each source in turn."""
    directory = inventory.find(system.cwd, system.env)
    hosts = tuple(
        sorted({*inventory.hosts(directory), *inventory.retired_hosts(directory)})
    )
    repos = sources.private_repos(system.http, creds.github_token, settings.owners)
    net = sources.tailnet(system.run, hosts)
    words = sources.common_words(system.words)
    operators = inventory.operators(directory)
    hosts_of_server = {
        f"{label}.{net.suffix}".lower() for label in settings.server_hosts
    }
    return gitleaks.Facts(
        repos=tuple(repos),
        tailnet=net,
        words=words,
        operators=operators,
        checkout=settings.workspace,
        vaults=tuple(sorted(name for name in vaults if name)),
        items=(gitleaks.ITEM,),
        endpoints=tuple(sorted(hosts_of_server)),
        names=tuple(sorted({gitleaks.KEYCHAIN, gitleaks.SERVICE, settings.server})),
        account=settings.account,
        session_sockets=settings.session_sockets,
    )


def summary(
    text: str,
    made: list[gitleaks.Rule],
    omitted: dict[str, int],
    facts: gitleaks.Facts,
) -> list[str]:
    """What the config covers. It holds no rule's regex and no config text."""
    canary = "present" if gitleaks.CANARY.search(text) else "missing"
    lines = [f"rules: {len(made) + 1}, with the canary {gitleaks.CANARY_ID} {canary}"]
    for category in gitleaks.CATEGORIES:
        count = sum(r.category == category for r in made)
        left = omitted.get(category)
        what = "ordinary name(s), matched only as owner/name"
        if category != "private-repo":
            what = "too common or short to match alone"
        tail = f", {left} {what}" if left else ""
        lines.append(f"  {category}: {count} rule(s){tail}")
    repos = sorted({f"{owner}/{name}" for owner, name in facts.repos})
    lines.append(f"repositories ({len(repos)}): {', '.join(repos)}")
    lines.append(
        f"hosts ({len(facts.tailnet.hosts)}): {', '.join(facts.tailnet.hosts)}"
    )
    return lines


def generate(system: System, out: TextIO, *, dry_run: bool) -> None:
    """Generate the config, check it, and write it unless DRY_RUN."""
    creds = config.credentials(system.env)
    settings = config.settings(system.env)
    server = connect.Connect(system.http, creds.op_host, creds.op_token)
    reached = connect.vaults(server)
    facts = _facts(system, creds, settings, reached)
    made, omitted = gitleaks.rules(facts)
    text = gitleaks.render(made)
    found = gitleaks.problem(text)
    out.writelines(f"{line}\n" for line in summary(text, made, omitted, facts))
    if found is not None:
        raise CliError(found)
    if gitleaks.VAULT not in reached:
        message = f"the Connect token doesn't reach the vault {gitleaks.VAULT}"
        raise CliError(message)
    status = connect.publish(server, reached[gitleaks.VAULT], text, write=not dry_run)
    if dry_run:
        verb = "is current" if status == "current" else f"would be {status}"
        out.write(f"dry run: {gitleaks.REF} {verb}; wrote nothing\n")
    else:
        out.write(f"{status}: {gitleaks.REF}\n")


def token_commands(  # the command's options, each one a fact
    system: System,
    out: TextIO,
    *,
    hosts: list[str],
    ops_session: str | None,
    runner_vms: list[str],
    dry_run: bool,
) -> None:
    """Print the script that creates the tokens of HOSTS, or of every agent host.

    With OPS_SESSION, an operator Mac, it also makes an ops-session token. Each
    of RUNNER_VMS, a CI runner VM, also gets a runner-VM token. With RUNNER_VMS
    and neither HOSTS nor OPS_SESSION, it makes only the runner-VM tokens.
    """
    server = config.connect_server(system.env)
    account = config.op_account(system.env)
    directory = inventory.find(system.cwd, system.env)
    agents = inventory.group(directory, tokens.AGENTS)
    controllers = inventory.group(directory, tokens.CONTROLLERS)
    runners = (
        tokens.select_runners(inventory.group(directory, tokens.CI), agents, runner_vms)
        if runner_vms
        else ()
    )
    # Runner VMs alone make a script of only their tokens.
    alone = bool(runners) and not hosts and ops_session is None
    chosen = () if alone else tokens.select(agents, hosts)
    script = tokens.commands(
        server,
        chosen,
        agents,
        controllers,
        tokens.stamp(system.now),
        account=account,
        ops_hosts=tokens.select_ops(controllers, chosen),
        session_host=ops_session,
        runner_vms=runners,
        dry_run=dry_run,
    )
    out.write(script)


def check(
    system: System,
    out: TextIO,
    *,
    host: str | None,
    ops_controller: bool,
    inventory_dir: Path | None,
) -> bool:
    """Print each vault as ok, missing or extra, and say whether all are ok.

    Each vault the plan grants read-write also gets a write probe, which
    prints one more line and must work for the check to pass.

    With OPS_CONTROLLER, the token is an ops-controller token, which reaches
    exactly the host-storage-* vaults, and HOST must be an operator Mac.

    With INVENTORY_DIR, the hosts and grants come from that inventory directory,
    not from the checkout the check runs in.
    """
    url, token = config.connect_credentials(system.env, system.run)
    name = host or system.hostname
    directory = (
        inventory.find(system.cwd, system.env)
        if inventory_dir is None
        else inventory.at(inventory_dir)
    )
    agents = inventory.group(directory, tokens.AGENTS)
    controllers = inventory.group(directory, tokens.CONTROLLERS)
    if ops_controller:
        plan = tokens.ops_controller(name, controllers)
    else:
        tokens.select(agents, [name])
        plan = tokens.grants(name, agents, controllers)
    server = connect.Connect(system.http, url, token)
    found = connect.listed_vaults(server)
    rows = tokens.reach(found, {g.vault for g in plan})
    out.write(f"{name}\n")
    out.writelines(f"  {status:<7} {vault}\n" for status, vault in rows)
    passed = all(status == "ok" for status, _ in rows)
    stamp = tokens.stamp(system.now)
    for grant in plan:
        # A vault the token doesn't reach has no id, and is already missing.
        if grant.permission == tokens.READ_WRITE and grant.vault in found:
            worked, line = connect.probe_write(server, found[grant.vault], stamp)
            out.write(f"{grant.vault} {line}\n")
            passed = passed and worked
    return passed


def run(argv: list[str] | None, system: System, out: TextIO, err: TextIO) -> int:
    """Run the command with ARGV, and return the exit status."""
    args = parser().parse_args(argv)
    try:
        if args.group in {"deploy", "dns"}:
            dispatch = deploy.dispatch if args.group == "deploy" else dns_smoke.dispatch
            return dispatch(args, system, out)
        if args.group == "s3":
            return s3.dispatch(args, system)
        if args.group == "o11y":
            return o11y.dispatch(args, system, out)
        if args.group == "gitleaks":
            generate(system, out, dry_run=args.dry_run)
        elif args.command == "token-commands":
            token_commands(
                system,
                out,
                hosts=args.host,
                ops_session=args.ops_session,
                runner_vms=args.runner_vm,
                dry_run=args.dry_run,
            )
        elif not check(
            system,
            out,
            host=args.host,
            ops_controller=args.ops_controller,
            inventory_dir=args.inventory,
        ):
            return 1
    except CliError as exc:
        what = (
            f"gitleaks generate: {exc}; wrote nothing"
            if args.group == "gitleaks"
            else f"{args.group} {args.command}: {exc}"
        )
        err.write(f"{PROG} {what}\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """The console entry point."""
    return run(argv, real_system(), sys.stdout, sys.stderr)
