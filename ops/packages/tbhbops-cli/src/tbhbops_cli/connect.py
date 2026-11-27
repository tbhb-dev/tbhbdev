"""The 1Password Connect server's API, for the one item the command writes.

The server answers over the tailnet at the https URL in OP_CONNECT_HOST. A
message never quotes that URL or a response, because the URL holds the
tailnet's domain.
"""

import json
import urllib.parse
from dataclasses import dataclass

from tbhbops.op_connect import SERVICE_HOST
from tbhbops_cli.common import CliError, Http, Run
from tbhbops_cli.gitleaks import FIELD, ITEM, REF, VAULT

# Quote every reserved character in an id, "/" too. quote() never escapes a
# letter, so a safe set of letters alone ("XXXX") quotes the same.
_SAFE = ""  # pragma: no mutate  # a safe set of only letters changes nothing


PROBE = "connect-check-probe-"


def host_url(host: str | None, run: Run) -> str:
    """Use HOST when given, or derive the full service name from Tailscale."""
    if host:
        return host
    message = (
        "cannot derive the Connect URL; set OP_CONNECT_HOST to the full name, "
        f"https://{SERVICE_HOST}.<MagicDNS suffix>"
    )
    try:
        status = json.loads(run(["tailscale", "status", "--json"]))
    except CliError, ValueError:
        raise CliError(message) from None
    suffix = status.get("MagicDNSSuffix") if isinstance(status, dict) else None
    if not isinstance(suffix, str) or not suffix:
        raise CliError(message)
    return f"https://{SERVICE_HOST}.{suffix}"


@dataclass(frozen=True)
class Connect:
    """A Connect server, and the token to call it with."""

    http: Http
    host: str
    token: str

    def send(self, method: str, path: str, body: object = None) -> tuple[int, bytes]:
        """Send the request to PATH, and return the status and the raw answer."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        data = None if body is None else json.dumps(body).encode()
        url = self.host.rstrip("/") + path
        return self.http(method, url, headers, data)

    def call(self, method: str, path: str, what: str, body: object = None) -> object:
        """Call PATH, and return the decoded answer. WHAT names the call."""
        status, raw = self.send(method, path, body)
        if not 200 <= status < 300:  # noqa: PLR2004  # the 2xx statuses
            message = f"{what} failed: HTTP {status}"
            raise CliError(message)
        try:
            answer: object = json.loads(raw)
        except ValueError:
            message = f"{what} printed something other than JSON"
            raise CliError(message) from None
        return answer


def _dicts(value: object) -> list[dict[str, object]]:
    entries: list[object] = value if isinstance(value, list) else []
    return [e for e in entries if isinstance(e, dict)]


def listed_vaults(connect: Connect) -> dict[str, str]:
    """The vaults the token reaches, as name to id. There may be none."""
    listed = _dicts(connect.call("GET", "/v1/vaults", "listing the vaults"))
    return {
        v["name"]: v["id"]
        for v in listed
        if isinstance(v.get("name"), str) and isinstance(v.get("id"), str)
    }


def vaults(connect: Connect) -> dict[str, str]:
    """The vaults the token reaches, as name to id, and at least one."""
    found = listed_vaults(connect)
    if not found:
        message = "the Connect token reaches no vaults"
        raise CliError(message)
    return found


def _ok(status: int) -> bool:
    return 200 <= status < 300  # noqa: PLR2004  # the 2xx statuses


def _item_id(raw: bytes) -> str:
    """The id in a created item's JSON, or "" if there isn't one."""
    try:
        made = json.loads(raw)
    except ValueError:
        return ""
    found = made.get("id") if isinstance(made, dict) else None
    return found if isinstance(found, str) else ""


def probe_write(connect: Connect, vault: str, stamp: str) -> tuple[bool, str]:
    """Try a write in VAULT, and return whether it worked and what to print.

    The probe creates a note titled connect-check-probe-STAMP with no secret
    in it, then deletes it. A vault's permission can't be read, so a write is
    the only way to know it.
    """
    title = f"{PROBE}{stamp}"
    base = f"/v1/vaults/{urllib.parse.quote(vault, safe=_SAFE)}/items"
    note = {
        "id": "notesPlain",
        "type": "STRING",
        "purpose": "NOTES",
        "value": "written and deleted by tbhbops connect check",
    }
    item = {
        "vault": {"id": vault},
        "title": title,
        "category": "SECURE_NOTE",
        "fields": [note],
    }
    status, raw = connect.send("POST", base, item)
    if not _ok(status):
        return False, f"write denied ({status})"
    item_id = _item_id(raw)
    if not item_id:
        # Connect answered without an id, so name the item by its title.
        return False, f"write ok, cleanup FAILED: delete item {title} by hand"
    status, _ = connect.send(
        "DELETE", f"{base}/{urllib.parse.quote(item_id, safe=_SAFE)}"
    )
    if not _ok(status):
        return False, f"write ok, cleanup FAILED: delete item {item_id} by hand"
    return True, "write ok"


def publish(connect: Connect, vault: str, text: str, *, write: bool) -> str:
    """Put TEXT in the REF field, and return `created`, `updated` or `current`.

    The item is created when it's missing. With WRITE false, it reads and
    changes nothing, and the answer says what WRITE true would do.
    """
    base = f"/v1/vaults/{urllib.parse.quote(vault, safe=_SAFE)}/items"
    query = urllib.parse.quote(f'title eq "{ITEM}"')
    summaries = _dicts(connect.call("GET", f"{base}?filter={query}", "listing items"))
    found = [s for s in summaries if s.get("title") == ITEM]
    if len(found) > 1:
        message = f"{VAULT} holds {len(found)} items named {ITEM}"
        raise CliError(message)
    field = {"id": FIELD, "label": FIELD, "type": "CONCEALED", "value": text}
    if not found:
        if write:
            item = {
                "vault": {"id": vault},
                "title": ITEM,
                "category": "SECURE_NOTE",
                "fields": [field],
            }
            connect.call("POST", base, f"creating {REF}", item)
        return "created"
    item_id = found[0].get("id")
    if not isinstance(item_id, str) or not item_id:
        message = f"listing items printed {ITEM} without an id"
        raise CliError(message)
    where = f"{base}/{urllib.parse.quote(item_id, safe=_SAFE)}"
    existing = connect.call("GET", where, f"reading {REF}")
    item = dict(existing) if isinstance(existing, dict) else {}
    fields = _dicts(item.get("fields"))
    if [f.get("value") for f in fields if f.get("label") == FIELD] == [text]:
        return "current"
    if write:
        item["fields"] = [f for f in fields if f.get("label") != FIELD] + [field]
        connect.call("PUT", where, f"updating {REF}", item)
    return "updated"
