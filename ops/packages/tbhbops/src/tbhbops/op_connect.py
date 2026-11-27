"""Deploy 1Password Connect, apps/op-connect, to its host.

Every step runs over SSH as one command on the host. The host's sudo allows
only the docker and docker-compose binaries, so anything that touches files
runs in a throwaway container with the deploy directory mounted at /d.
Docker on the host won't create a missing bind-mount source, so `stage` first
runs one container with the parent directory mounted that only creates the
deploy directory, mode 0700 and owned by root. The
credentials file reaches that container on SSH's stdin, straight from `op`,
and never lands in an argument, a variable on the host, or a log.

The steps, in order: preflight, stage, secrets, up, login, lock, serve,
health. The ones that change the host take `--confirm <step>`.
"""

import argparse
import json
import posixpath
import re
import shlex
import shutil
import subprocess  # nosec B404: every command is an argv list, never a shell string
from collections.abc import Callable, Mapping
from typing import TextIO

HOST_ENV = "OP_CONNECT_TARGET"
PROJECT = "op-connect"
DOCKER = "/usr/local/bin/docker"
COMPOSE = "/usr/local/bin/docker-compose"
# The opuser account in the connect-api and connect-sync images.
OPUSER = "999:999"
SERVICE = "svc:op-connect"
SERVICE_HOST = "op-connect"
DIR_ENV = "OP_CONNECT_DIR"
# Where the macOS app keeps its CLI, which isn't on PATH unless linked.
MACOS_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
STEPS = ("preflight", "stage", "secrets", "up", "login", "lock", "serve", "health")
CHANGES = ("stage", "secrets", "up", "serve")

Run = Callable[[list[str], bytes | None], bytes]

NO_TS_IMAGE = "compose.yaml has no tailscale/tailscale image"
NO_NODE_KEY = "tailscale status has no node key"
NOT_CREDENTIALS = "the credentials document is not a JSON object"
BAD_DIRECTORY = (
    "the deploy directory must be one plain name under a directory other than /"
)

_TS_IMAGE = re.compile(r"^\s*image:\s*(tailscale/tailscale:\S+)\s*$", re.MULTILINE)
_LOGIN_URL = re.compile(r"https://login\.tailscale\.com/a/[0-9a-z]+")

# $1 is the deploy directory's name, checked by split_directory. mkdir -m
# doesn't change a directory that already exists, so chmod sets the mode.
MKDIR_SCRIPT = 'set -eu; mkdir -p "/d/$1"; chmod 700 "/d/$1"'
STAGE_SCRIPT = (
    "set -eu; umask 077; "
    "mkdir -p /d/data /d/secrets /d/ts-state; "
    f"chown {OPUSER} /d/data /d/secrets; "
    "cat > /d/compose.yaml.tmp; chmod 644 /d/compose.yaml.tmp; "
    "mv -f /d/compose.yaml.tmp /d/compose.yaml"
)
SECRETS_SCRIPT = (
    "set -eu; umask 077; f=/d/secrets/.credentials.tmp; "
    # Compose creates a missing bind-mount source as a directory, and mv
    # would then put the file inside it.
    "test ! -d /d/secrets/1password-credentials.json; "
    'cat > "$f"; test -s "$f"; '
    f'chown {OPUSER} "$f"; chmod 400 "$f"; '
    'mv -f "$f" /d/secrets/1password-credentials.json'
)


def ssh(host: str, remote: list[str]) -> list[str]:
    """Return the local argv that runs `remote` on the host."""
    return ["ssh", "--", host, shlex.join(remote)]


def compose(directory: str, *args: str) -> list[str]:
    """Return the host argv for a docker-compose command on the project."""
    return [
        "sudo",
        "-n",
        COMPOSE,
        "-p",
        PROJECT,
        "-f",
        f"{directory}/compose.yaml",
        *args,
    ]


def utility(directory: str, image: str, script: str, *args: str) -> list[str]:
    """Return the host argv that runs `script` with the directory at /d."""
    return [
        "sudo",
        "-n",
        DOCKER,
        "run",
        "-i",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{directory}:/d",
        "--entrypoint",
        "sh",
        image,
        "-c",
        script,
        *args,
    ]


def split_directory(directory: str) -> tuple[str, str]:
    """Return the deploy directory's parent and its one-component name."""
    parent, name = posixpath.split(directory)
    # The parent is mounted into the mkdir container, so never the host root.
    if name in {"", ".", ".."} or parent == "/":
        raise ValueError(BAD_DIRECTORY)
    return parent, name


def ts_image(compose_text: str) -> str:
    """Return the pinned tailscale image from the compose file."""
    match = _TS_IMAGE.search(compose_text)
    if match is None:
        raise ValueError(NO_TS_IMAGE)
    return match.group(1)


def login_url(logs: str) -> str | None:
    """Return the last login URL the sidecar printed, if any."""
    urls = _LOGIN_URL.findall(logs)
    return urls[-1] if urls else None


def node_key(status_json: str) -> str:
    """Return the sidecar's node key from `tailscale status --json`."""
    key = str(json.loads(status_json)["Self"]["PublicKey"])
    if not key.startswith("nodekey:"):
        raise ValueError(NO_NODE_KEY)
    return key


def check_credentials(data: bytes) -> None:
    """Fail unless the document is a JSON object, without echoing it."""
    try:
        parsed = json.loads(data)
    except ValueError:
        # A JSONDecodeError holds the whole document, so it must not ride
        # along as this error's context.
        raise TypeError(NOT_CREDENTIALS) from None
    if not isinstance(parsed, dict):
        raise TypeError(NOT_CREDENTIALS)


def heartbeat_url(local_status_json: str) -> str:
    """Return the Service's heartbeat URL from this machine's tailnet name."""
    suffix = json.loads(local_status_json)["MagicDNSSuffix"]
    return f"https://{SERVICE_HOST}.{suffix}/heartbeat"


def run_subprocess(argv: list[str], stdin: bytes | None) -> bytes:
    """Run argv, pass stderr through, and return stdout."""
    return subprocess.run(  # nosec B603: argv list, no shell
        argv, input=stdin, stdout=subprocess.PIPE, check=True
    ).stdout


def _text(raw: bytes) -> str:
    return raw.decode()


def _ts(host: str, directory: str, run: Run, *args: str) -> str:
    """Run a command in the ts sidecar and return its output."""
    return _text(run(ssh(host, compose(directory, "exec", "-T", "ts", *args)), None))


def _preflight(host: str, directory: str, run: Run, out: TextIO) -> None:
    probe = (
        f"ls -ld {shlex.quote(directory)} 2>/dev/null || echo 'no deploy directory yet'"
    )
    remotes = (
        ["sudo", "-n", DOCKER, "version", "--format", "{{.Server.Version}}"],
        ["sudo", "-n", COMPOSE, "version", "--short"],
        ["sudo", "-n", DOCKER, "network", "ls", "--format", "{{.Name}}"],
        ["sh", "-c", probe],
    )
    out.writelines(_text(run(ssh(host, remote), None)) for remote in remotes)


def _stage(host: str, directory: str, compose_text: str, run: Run, out: TextIO) -> None:
    image = ts_image(compose_text)
    parent, name = split_directory(directory)
    run(ssh(host, utility(parent, image, MKDIR_SCRIPT, "sh", name)), None)
    run(ssh(host, utility(directory, image, STAGE_SCRIPT)), compose_text.encode())
    out.write(f"staged compose.yaml in {directory}\n")


def _secrets(
    host: str, directory: str, compose_text: str, run: Run, out: TextIO,
    account: str, vault: str, item: str,
) -> None:
    image = ts_image(compose_text)
    get = ["op", "--account", account, "document", "get", item, "--vault", vault]
    document = run(get, None)
    check_credentials(document)
    run(ssh(host, utility(directory, image, SECRETS_SCRIPT)), document)
    out.write("wrote the credentials file\n")


def _up(host: str, directory: str, run: Run, out: TextIO) -> None:
    out.write(_text(run(ssh(host, compose(directory, "up", "-d")), None)))


def _login(host: str, directory: str, run: Run, out: TextIO) -> None:
    state = json.loads(_ts(host, directory, run, "tailscale", "status", "--json"))
    if state["BackendState"] == "Running":
        out.write("the sidecar is signed in\n")
        return
    logs = run(ssh(host, compose(directory, "logs", "--no-color", "ts")), None)
    url = login_url(_text(logs))
    if url is None:
        out.write(f"no login URL yet; state {state['BackendState']}\n")
    else:
        out.write(f"open this URL and sign the sidecar in: {url}\n")


def _lock(host: str, directory: str, run: Run, out: TextIO) -> None:
    key = node_key(_ts(host, directory, run, "tailscale", "status", "--json"))
    out.write(f"run on an authorized signing host: tailscale lock sign {key}\n")


def _serve(host: str, directory: str, run: Run, out: TextIO) -> None:
    target = "http://api:8080"
    _ts(
        host,
        directory,
        run,
        "tailscale",
        "serve",
        f"--service={SERVICE}",
        "--https=443",
        target,
    )
    out.write(f"serving {SERVICE} on 443\n")


def _health(host: str, directory: str, run: Run, out: TextIO) -> None:
    for name in ("api", "sync"):
        body = _ts(host, directory, run, "wget", "-qO-", f"http://{name}:8080/health")
        out.write(f"{name}: {body.strip()}\n")
    tailscale = shutil.which("tailscale") or MACOS_TAILSCALE
    url = heartbeat_url(_text(run([tailscale, "status", "--json"], None)))
    out.write(f"{SERVICE}: {_text(run(['curl', '-fsS', url], None)).strip()}\n")


def parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    p = argparse.ArgumentParser(prog="op-connect")
    p.add_argument("step", choices=STEPS)
    p.add_argument("--confirm")
    return p


def main(
    argv: list[str], env: Mapping[str, str], compose_text: str, run: Run, out: TextIO
) -> int:
    """Run one deploy step and return the exit status."""
    args = parser().parse_args(argv)
    host = env.get(HOST_ENV, "")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
        out.write(f"set {HOST_ENV} to the deploy host name\n")
        return 2
    # Any default that isn't an absolute path is refused the same way.
    directory = env.get(DIR_ENV, "")  # pragma: no mutate
    if not directory.startswith("/"):
        out.write(f"set {DIR_ENV} to the deploy directory's absolute path on {host}\n")
        return 2
    if args.step in CHANGES and args.confirm != args.step:
        out.write(f"{args.step} changes {host}; pass --confirm {args.step}\n")
        return 2
    if args.step == "secrets":
        keys = ("OP_CONNECT_DEPLOY_ACCOUNT", "OP_CONNECT_DEPLOY_VAULT", "OP_CONNECT_DEPLOY_ITEM")
        account, vault, item = (env.get(key, "") for key in keys)
        if not all((account, vault, item)):
            out.write(f"set {', '.join(keys)} to the credentials source\n")
            return 2
        _secrets(host, directory, compose_text, run, out, account, vault, item)
        return 0
    steps: dict[str, Callable[[], None]] = {
        "preflight": lambda: _preflight(host, directory, run, out),
        "stage": lambda: _stage(host, directory, compose_text, run, out),
        "up": lambda: _up(host, directory, run, out),
        "login": lambda: _login(host, directory, run, out),
        "lock": lambda: _lock(host, directory, run, out),
        "serve": lambda: _serve(host, directory, run, out),
        "health": lambda: _health(host, directory, run, out),
    }
    steps[args.step]()
    return 0
