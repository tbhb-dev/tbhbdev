"""Where the private gitleaks config's other facts come from.

A source that fails or finds nothing stops the generator before it writes
anything, and no message quotes what a source found.
"""

import json
import shutil
import subprocess  # nosec B404: every command is an argv list, never a shell string
from pathlib import Path

from tbhbops_cli.common import CliError, Http, Run
from tbhbops_cli.gitleaks import TECH_WORDS, Tailnet

GRAPHQL = "https://api.github.com/graphql"
# gh lists at most this many, so a list that long may be cut short.
REPO_LIMIT = 1000
PAGE = 100
QUERY = (
    "query($owner: String!, $after: String, $first: Int!) {"
    " repositoryOwner(login: $owner) {"
    " repositories(first: $first, after: $after, privacy: PRIVATE,"
    " ownerAffiliations: OWNER) {"
    " nodes { name } pageInfo { hasNextPage endCursor } } } }"
)
WORDS = Path("/usr/share/dict/words")
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _json(raw: bytes, source: str) -> object:
    try:
        data: object = json.loads(raw)
    except ValueError:
        message = f"{source} printed something other than JSON"
        raise CliError(message) from None
    return data


def _object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _page(
    http: Http, token: str, owner: str, after: str | None
) -> tuple[list[str], str | None]:
    """One page of OWNER's private repositories, and the cursor after it."""
    source = f"the GitHub repository list for {owner}"
    body = json.dumps(
        {"query": QUERY, "variables": {"owner": owner, "after": after, "first": PAGE}}
    ).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "tbhbops",
    }
    status, raw = http("POST", GRAPHQL, headers, body)
    if status != 200:  # noqa: PLR2004  # HTTP OK
        message = f"{source} failed: HTTP {status}"
        raise CliError(message)
    data = _object(_json(raw, source))
    owned = _object(_object(data.get("data")).get("repositoryOwner"))
    repositories = _object(owned.get("repositories"))
    nodes = repositories.get("nodes")
    entries: list[object] = nodes if isinstance(nodes, list) else []
    names = [_object(n).get("name") for n in entries]
    if not isinstance(nodes, list) or not all(isinstance(n, str) and n for n in names):
        message = f"{source} printed something other than named repositories"
        raise CliError(message)
    info = _object(repositories.get("pageInfo"))
    cursor = info.get("endCursor")
    if info.get("hasNextPage") is not True:
        return [str(n) for n in names], None
    if not isinstance(cursor, str) or not cursor:
        message = f"{source} printed something other than named repositories"
        raise CliError(message)
    return [str(n) for n in names], cursor


def private_repos(
    http: Http, token: str, owners: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Each owner's private repositories as (owner, name); the first has some."""
    found: list[tuple[str, str]] = []
    for owner in owners:
        names: list[str] = []
        cursor: str | None = None
        while True:
            page, cursor = _page(http, token, owner, cursor)
            names += page
            if len(names) >= REPO_LIMIT:
                message = (
                    f"the GitHub repository list for {owner} reached its limit "
                    f"of {REPO_LIMIT} and may miss some"
                )
                raise CliError(message)
            if cursor is None:
                break
        if not names and owner == owners[0]:
            message = (
                f"the GitHub repository list for {owner} has no private repositories"
            )
            raise CliError(message)
        found += [(owner, name) for name in names]
    return found


def run_command(argv: list[str]) -> str:
    """Run ARGV and return its output. A failure's message holds no output."""
    try:
        done = subprocess.run(  # noqa: S603  # nosec B603: an argv list, no shell
            argv,
            capture_output=True,
            text=True,
            check=False,  # pragma: no mutate  # None and an omitted check are False
        )
    except OSError:
        message = f"cannot run {argv[0]}"
        raise CliError(message) from None
    if done.returncode != 0:
        message = f"{argv[0]} failed: exit {done.returncode}"
        raise CliError(message)
    return done.stdout


def _tailscale() -> str:
    found = shutil.which("tailscale")
    if found is not None:
        return found
    if Path(TAILSCALE_APP).is_file():
        return TAILSCALE_APP
    message = f"tailscale isn't on PATH and {TAILSCALE_APP} doesn't exist"
    raise CliError(message)


def tailnet(run: Run, hosts: tuple[str, ...]) -> Tailnet:
    """The tailnet's MagicDNS suffix and name, from `tailscale status`.

    The inventory leaves both out on purpose, so tailscale is their one
    source. The hosts are the inventory's, not tailscale's peers.
    """
    source = "tailscale status"
    status = _object(_json(run([_tailscale(), "status", "--json"]).encode(), source))
    suffix = status.get("MagicDNSSuffix")
    name = _object(status.get("CurrentTailnet")).get("Name")
    for what, value in (("MagicDNSSuffix", suffix), ("CurrentTailnet.Name", name)):
        if not isinstance(value, str) or not value:
            message = f"{source} has no {what}"
            raise CliError(message)
    return Tailnet(str(suffix), str(name), hosts)


def common_words(path: Path) -> frozenset[str]:
    """The words too common for a name to match alone, lowercased."""
    try:
        words = frozenset(path.read_bytes().decode().lower().split())
    except OSError, UnicodeError:
        message = f"cannot read {path}"
        raise CliError(message) from None
    if not words:
        message = f"{path} lists no words"
        raise CliError(message)
    return words | TECH_WORDS
