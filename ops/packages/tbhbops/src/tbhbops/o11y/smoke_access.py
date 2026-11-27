"""Grafana access and provisioning expectations for the ops stack."""

import ipaddress
import re

from tbhbops.o11y.receipt import decode
lazy from tbhbops.o11y.validate import Document

FOLDER = "o11y-agents-folder"
AGENTS = (
    "o11y-agents",
    "o11y-agents-capacity",
    "o11y-agents-claude-code",
    "o11y-agents-codex",
    "o11y-agents-runs",
    "o11y-agents-traces",
)
HEALTH = "o11y-health"
DASHBOARD_FILES: dict[str, str] = {
    "o11y-agents": "overview.json",
    "o11y-agents-capacity": "capacity.json",
    "o11y-agents-claude-code": "claude-code.json",
    "o11y-agents-codex": "codex.json",
    "o11y-agents-runs": "runs.json",
    "o11y-agents-traces": "traces.json",
    "o11y-github-activity": "github-activity.json",
    HEALTH: "stack-health.json",
}
RULES = (
    "ca-backup-unhealthy",
    "o11y-alloy-export",
    "o11y-capacity-exhaustion",
    "o11y-capacity-shed",
    "o11y-capacity-stale",
    "o11y-capacity-stop",
    "o11y-capacity-warn",
    "o11y-disk-low",
    "o11y-ingest-dropped",
    "o11y-log-rate",
    "o11y-metrics-queries-failing",
    "o11y-rustfs-memory",
    "o11y-service-down",
)
CONTACT_POINT_COUNT = 2
SOURCES = ("loki", "mimir", "tempo")
OWNER = "owner@example.invalid"
VIEWER = "viewer@example.invalid"


def user(body: bytes, login: str, *, admin: bool = False) -> bool:
    data = decode(body)
    return (
        isinstance(data, dict)
        and data.get("login") == login
        and data.get("isGrafanaAdmin") is admin
    )


def orgs(body: bytes, role: str, *, recovery: bool = False) -> bool:
    data = decode(body)
    return (
        isinstance(data, list)
        and len(data) == 1
        and isinstance(data[0], dict)
        and data[0].get("role") == role
        and (recovery or data[0].get("orgId") == 1)
    )


def owner_model(services: dict[str, Document]) -> bool:
    try:
        owner = services["grafana-owner"]
        return (
            owner["user"] == "472:472"
            and owner["depends_on"]["grafana"]["condition"] == "service_healthy"
            and any(
                mount["target"] == "/run/o11y/grafana_admin_password"
                and mount["read_only"] is True
                for mount in owner["volumes"]
            )
        )
    except KeyError, TypeError:
        return False


def whitelist(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text.strip())
    except ipaddress.AddressValueError:
        return False
    return True


def identifiers(body: bytes, expected: tuple[str, ...]) -> bool:
    data = decode(body)
    try:
        return isinstance(data, list) and sorted(item["uid"] for item in data) == list(
            expected
        )
    except KeyError, TypeError:
        return False


def dashboard_checkpoint(responses: dict[str, bytes]) -> bool:
    """Only the deployed folder layout is supported; all dashboards are provisioned."""
    try:
        search = decode(responses["/api/search?type=dash-db"])
        if not isinstance(search, list):
            return False
        if sorted((d["uid"], d.get("folderUid") or "") for d in search) != [
            *((uid, FOLDER) for uid in AGENTS),
            ("o11y-github-activity", ""),
            (HEALTH, ""),
        ]:
            return False
        folder = decode(responses[f"/api/folders/{FOLDER}"])
        if folder["uid"] != FOLDER or folder["title"] != "Agents":
            return False
        for uid in (*AGENTS, "o11y-github-activity", HEALTH):
            data = decode(responses[f"/api/dashboards/uid/{uid}"])
            expected_folder = FOLDER if uid in AGENTS else ""
            if (
                data["dashboard"]["uid"] != uid
                # Grafana v13.2.4 pkg/api/dashboard.go:200-208 reports the
                # source file when allowUiUpdates makes meta.provisioned false.
                or data["meta"]["provisionedExternalId"] != DASHBOARD_FILES[uid]
                or (data["meta"].get("folderUid") or "") != expected_folder
            ):
                return False
    except KeyError, TypeError, AttributeError:
        return False
    return True


def contact_points(body: bytes) -> bool:
    data = decode(body)
    try:
        return (
            isinstance(data, list)
            and len(data) == CONTACT_POINT_COUNT
            and all(p["name"] == "o11y" and p["provenance"] == "file" for p in data)
            and sum(
                p["uid"] == "o11y-discord"
                and p["type"] == "discord"
                and p["settings"]["url"] == "[REDACTED]"
                for p in data
            )
            == 1
            and sum(
                p["uid"] == "o11y-email"
                and p["type"] == "email"
                and p["settings"]["addresses"] == "smoke-to@example.invalid"
                for p in data
            )
            == 1
        )
    except KeyError, TypeError:
        return False


def decrypted(body: bytes, webhook: str) -> bool:
    try:
        matches = [
            receiver
            for point in decode(body)["contactPoints"]
            for receiver in point["receivers"]
            if receiver["uid"] == "o11y-discord"
            and receiver["settings"]["url"] == webhook
        ]
        return len(matches) == 1
    except KeyError, TypeError:
        return False


def home(body: bytes, *, redirect: bool = False) -> bool:
    data = decode(body)
    if not isinstance(data, dict):
        return False
    if redirect:
        value = data.get("redirectUri")
        return isinstance(value, str) and value.startswith(f"/d/{HEALTH}/")
    return data.get("homeDashboardUID") == HEALTH


def provisioning(text: str) -> str | None:
    lines = [line for line in text.splitlines() if "provisioning" in line.lower()]
    if not lines:
        return "4: provisioning-log-missing"
    if any(re.search(r"level=(error|warn)", line.lower()) for line in lines):
        return "4: provisioning-log-error"
    return None
