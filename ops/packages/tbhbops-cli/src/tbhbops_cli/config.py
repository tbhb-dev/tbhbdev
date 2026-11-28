"""What the command reads from its environment and from the tbhbdev config.

The tbhbdev config is the file `tbhbdev host bootstrap` reads:
`tbhbdev/config.toml` under `$XDG_CONFIG_HOME`, or under `.config` in the
home directory. Ansible's agent_account role installs it on each agent host.
This command reads these values from it, as bootstrap does:

    [gitleaks] owners            the owners whose private repositories it covers
    [gitleaks] session_sockets   optional; session socket paths to keep out
    [gitleaks] item_ref          optional; the 1Password reference for the config
    [op] account                 the 1Password account
    [connect] server             the Connect server's name
    [connect] server_hosts       the hosts that run it, each a DNS label
    [workspace] root             optional; the checkouts' root below home

A message names the file and the `[table] key`, and never a value.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
lazy from collections.abc import Mapping

from tbhbops_cli import connect, gitleaks
from tbhbops_cli.common import CliError, Run

# The names of the three settings that come from the environment, and what
# each one is for. The first two are credentials.
OP_TOKEN = "OP_CONNECT_TOKEN"  # nosec B105  # a variable's name
GITHUB_TOKEN = "TBHBOPS_GITHUB_TOKEN"  # nosec B105  # a variable's name
OP_HOST = "OP_CONNECT_HOST"
ENVIRONMENT = (
    (
        OP_TOKEN,
        (
            "a 1Password Connect token with write access to the scan config item"
            " (default scanner-policy)"
        ),
    ),
    (GITHUB_TOKEN, "a read-only GitHub token that lists the private repositories"),
    (OP_HOST, "the Connect server's https URL"),
)
# What each of `[connect] server_hosts` must be, so that its endpoint's host
# is that one label under the tailnet's suffix.
DNS_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_REF_PARTS = 3


@dataclass(frozen=True)
class Credentials:
    """The environment's settings. Neither token ever reaches a message."""

    op_token: str
    github_token: str
    op_host: str


@dataclass(frozen=True)
class Settings:
    """The values from the tbhbdev config."""

    owners: tuple[str, ...]
    session_sockets: tuple[str, ...]
    account: str
    server: str
    server_hosts: tuple[str, ...]
    workspace: PurePosixPath | None
    item_ref: gitleaks.ItemRef = gitleaks.DEFAULT_REF


def _missing(env: Mapping[str, str], wanted: tuple[tuple[str, str], ...]) -> str:
    """The settings in WANTED that ENV lacks, each with its use, or ""."""
    return "; ".join(f"{name} ({use})" for name, use in wanted if not env.get(name))


def credentials(env: Mapping[str, str]) -> Credentials:
    """The settings in ENV, or CliError naming each one that is missing."""
    missing = _missing(env, ENVIRONMENT)
    if missing:
        message = (
            f"missing from the environment: {missing}. @tbhb runs this command "
            "under their own session until they exist"
        )
        raise CliError(message)
    return Credentials(env[OP_TOKEN], env[GITHUB_TOKEN], env[OP_HOST])


def connect_credentials(env: Mapping[str, str], run: Run) -> tuple[str, str]:
    """The Connect server's URL and the token in ENV, or CliError.

    A host that checks its own token may derive the URL from Tailscale.
    """
    missing = _missing(env, tuple(e for e in ENVIRONMENT if e[0] == OP_TOKEN))
    if missing:
        message = f"missing from the environment: {missing}"
        raise CliError(message)
    return connect.host_url(env.get(OP_HOST), run), env[OP_TOKEN]


def config_path(env: Mapping[str, str]) -> Path:
    """The config file: in `$XDG_CONFIG_HOME`, or in `.config` under home."""
    base = env.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "tbhbdev" / "config.toml"


def _load(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_bytes().decode())
    except FileNotFoundError:
        message = f"{path} doesn't exist; create it"
        raise CliError(message) from None
    except OSError, UnicodeError, tomllib.TOMLDecodeError:
        # The parser's message quotes the offending line, so it stays out.
        message = f"cannot read {path} as TOML"
        raise CliError(message) from None


def _table(data: dict[str, object], path: Path, name: str) -> dict[str, object]:
    section = data.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        message = f"{path}: {name} must be a table"
        raise CliError(message)
    return section


def _strings(
    data: dict[str, object], path: Path, table: str, key: str
) -> tuple[str, ...]:
    value = _table(data, path, table).get(key)
    entries: list[object] = value if isinstance(value, list) else []
    if not entries or not all(isinstance(e, str) and e for e in entries):
        message = f"{path}: [{table}] {key} must be a non-empty list of strings"
        raise CliError(message)
    return tuple(str(e) for e in entries)


def _string(data: dict[str, object], path: Path, table: str, key: str) -> str:
    value = _table(data, path, table).get(key)
    if not isinstance(value, str) or not value:
        message = f"{path}: [{table}] {key} must be a non-empty string"
        raise CliError(message)
    return value


def _workspace(data: dict[str, object], path: Path) -> PurePosixPath | None:
    section = data.get("workspace")
    if section is None:
        return None
    root = _table(data, path, "workspace").get("root")
    found = PurePosixPath(root) if isinstance(root, str) else PurePosixPath("/")
    if found.is_absolute() or not found.parts or ".." in found.parts:
        message = f"{path}: [workspace] root must be a relative path below home"
        raise CliError(message)
    return found


def _item_ref(data: dict[str, object], path: Path) -> gitleaks.ItemRef:
    if "item_ref" not in _table(data, path, "gitleaks"):
        return gitleaks.DEFAULT_REF
    raw = _string(data, path, "gitleaks", "item_ref")
    body = raw.removeprefix(gitleaks.REF_SCHEME)
    parts = body.split("/")
    if (
        not raw.startswith(gitleaks.REF_SCHEME)
        or len(parts) != _REF_PARTS
        or not all(parts)
        or ".." in parts
        or any(c.isspace() or c in "?#" for c in body)
    ):
        message = (
            f"{path}: [gitleaks] item_ref must be "
            f"{gitleaks.REF_SCHEME}<vault>/<item>/<field>"
        )
        raise CliError(message)
    return gitleaks.ItemRef(parts[0], parts[1], parts[2])


def connect_server(env: Mapping[str, str]) -> str:
    """The Connect server's name, `[connect] server` in the tbhbdev config."""
    path = config_path(env)
    return _string(_load(path), path, "connect", "server")


def op_account(env: Mapping[str, str]) -> str:
    """The 1Password account, `[op] account` in the tbhbdev config."""
    path = config_path(env)
    return _string(_load(path), path, "op", "account")


def settings(env: Mapping[str, str]) -> Settings:
    """The tbhbdev config's values, or CliError naming what is wrong."""
    path = config_path(env)
    data = _load(path)
    server_hosts = _strings(data, path, "connect", "server_hosts")
    for number, name in enumerate(server_hosts, start=1):
        if not DNS_LABEL.fullmatch(name):
            message = (
                f"{path}: entry {number} of [connect] server_hosts isn't a DNS label"
            )
            raise CliError(message)
    sockets = (
        _strings(data, path, "gitleaks", "session_sockets")
        if "session_sockets" in _table(data, path, "gitleaks")
        else ()
    )
    return Settings(
        owners=_strings(data, path, "gitleaks", "owners"),
        session_sockets=sockets,
        account=_string(data, path, "op", "account"),
        server=_string(data, path, "connect", "server"),
        server_hosts=server_hosts,
        workspace=_workspace(data, path),
        item_ref=_item_ref(data, path),
    )
