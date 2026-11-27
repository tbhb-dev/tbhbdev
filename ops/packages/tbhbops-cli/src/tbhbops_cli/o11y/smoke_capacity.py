"""Capacity histories, provisioned panels and documented deletion rehearsal."""

# pylint: disable=duplicate-code  # independent upload protocol contract

import json
import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from tbhbops.o11y import smoke_access as access
from tbhbops.o11y import smoke_capacity as capacity
from tbhbops.o11y import smoke_export as export
from tbhbops.o11y import smoke_query as query
from tbhbops.o11y.receipt import decode

from tbhbops_cli.o11y import smoke_checks as checks
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke import SmokeError, present, require

if TYPE_CHECKING:
    from tbhbops.o11y.validate import Document

    from tbhbops_cli.o11y.smoke import Smoke

CAPACITY_TIMEOUT = 120
HTTP_OK = 200
HTTP_NOT_FOUND = 404
METADATA_COUNT = 5
CODEX_RATE_BOUNDS = (0.999999, 1.000001)
UPLOAD_CURL_TIMEOUT = 30
RELOAD_TIMEOUT = 30


def panel(run: Smoke, title: str, now: int) -> list[Document] | None:
    code, body = checks.request(run, "/api/dashboards/uid/o11y-agents-capacity")
    if code != HTTP_OK:
        return None
    prepared = capacity.panel_request(body, title, now)
    if prepared is None:
        return None
    definition, data = prepared
    code, body = checks.request(
        run, "/api/ds/query", data=data, timeout=checks.EVAL_TIMEOUT
    )
    if code != HTTP_OK:
        return None
    return (
        capacity.live_windows(body, definition)
        if title == "Live windows"
        else query.numbers(body)
    )


def upload(run: Smoke, fixture: bytes, name: str) -> None:
    run.fs.write_atomic(run.out / name, fixture, 0o600)
    result = run.inside(
        "http://alloy:4318/v1/metrics",
        body=True,
        args=(
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
        ),
        input_data=fixture.decode(),
        curl_timeout=UPLOAD_CURL_TIMEOUT,
    )
    require(result.code == 0, "capacity: upload-exec")
    body, separator, status = result.stdout.rstrip("\n").rpartition("\n")
    require(bool(separator) and status == "200", "capacity: upload-http")
    require(export.upload(body.encode()), "capacity: rejected-points")


def smoke(run: Smoke, source: Path) -> None:
    require(
        query.healthy_rules(
            checks.get(run, checks.RULES_PATH, "capacity"),
            checks.rule_titles(run, source, capacity=True),
            capacity=True,
        ),
        "capacity: empty-rules-normal",
    )
    for kind in capacity.KINDS:
        require(checks.quiet(run, "o11y-capacity-" + kind), "capacity: empty-condition")
    now = int(run.system.epoch())
    history = capacity.histories(now)
    run.fs.write_atomic(run.out / "capacity.json", json.dumps(history), 0o600)
    upload(run, capacity.phase(history, resets=False), "capacity-no-reset.json")
    require(
        poll(
            run.system,
            CAPACITY_TIMEOUT,
            lambda: capacity.windows(panel(run, "Live windows", now), "missing-reset"),
        ),
        "capacity: missing-reset-columns",
    )
    upload(run, capacity.phase(history, resets=True), "capacity-reset.json")
    require(
        poll(
            run.system,
            CAPACITY_TIMEOUT,
            lambda: capacity.ingested(
                checks.mimir(run, '{__name__=~"agent_capacity_.*"}')
            ),
        ),
        "capacity: exact-metrics-labels",
    )
    for kind, label, expected in (
        ("warn", "agent_plan_window", ["5h", "7d"]),
        ("shed", "agent_plan_window", ["7d"]),
        ("stop", "agent_plan_window", list[str]()),
        ("stale", "agent_plan_vendor", ["google"]),
    ):
        require(
            poll(
                run.system,
                CAPACITY_TIMEOUT,
                lambda kind=kind, label=label, expected=expected: checks.fires(
                    run, "o11y-capacity-" + kind, label, expected
                ),
            ),
            "capacity: populated-condition",
        )
    require(
        poll(
            run.system,
            CAPACITY_TIMEOUT,
            lambda: capacity.windows(panel(run, "Live windows", now), "ready"),
        ),
        "capacity: joined-live-windows",
    )
    require(
        capacity.windows(panel(run, "Live windows", now + 1200), "inventory"),
        "capacity: inventory-numeric-columns",
    )
    require(
        panel(run, "Live windows", now + 8 * 86400) == []
        and capacity.windows(panel(run, "Live windows", now + 1200), "inventory"),
        "capacity: expired-inventory",
    )
    for title in (
        "Freshness and source",
        "Projected utilization at reset (3 h or 12 h of data, capped at 100%)",
        "Utilization history by account and window",
        "Claude Code tokens per second (fleet total)",
        "Claude Code cost per hour (fleet total)",
        "Codex tokens per second",
    ):
        rows = panel(run, title, now)
        require(panel_ok(title, rows), "capacity: panel-values")
    run.emit(
        "ok capacity: four conditions, exact OTLP names, joined "
        "windows, metadata, projection and burn pass"
    )


def history_ok(rows: list[Document]) -> bool:
    return bool(rows) and all(
        any(value is not None for value in row["values"])
        and all(value is None or 0 <= value <= 1 for value in row["values"])
        for row in rows
    )


def panel_ok(title: str, rows: list[Document] | None) -> bool:
    try:
        if rows is None:
            return False
        if title == "Freshness and source":
            return (
                len(rows) == METADATA_COUNT
                and all(r["labels"]["agent_account"] == "primary" for r in rows)
                and all(r["labels"]["agent_plan_window"] != "other" for r in rows)
                and sorted({r["labels"]["agent_capacity_freshness"] for r in rows})
                == ["carried", "fresh"]
                and sorted({r["labels"]["agent_capacity_source"] for r in rows})
                == ["claude_statusline", "codex_rollout"]
            )
        if (
            title
            == "Projected utilization at reset (3 h or 12 h of data, capped at 100%)"
        ):
            return bool(rows) and all(
                r["labels"]["agent_plan_window"]
                and (
                    r["values"][0] is None
                    or (
                        isinstance(r["values"][0], float) and math.isnan(r["values"][0])
                    )
                )
                for r in rows
            )
        if title == "Utilization history by account and window":
            return history_ok(rows)
        if len(rows) != 1:
            return False
        samples: list[float] = rows[0]["values"]
        return (
            CODEX_RATE_BOUNDS[0] < samples[0] < CODEX_RATE_BOUNDS[1]
            if title == "Codex tokens per second"
            else samples[0] > 0
        )
    except KeyError, TypeError, IndexError:
        return False


def reload(run: Smoke, kind: str) -> None:
    try:
        code, _ = run.system.admin_http.request(
            checks.smoke_stages.GRAFANA + "/api/admin/provisioning/" + kind + "/reload",
            dict(run.sentinels.values)["grafana_admin_password"],
            RELOAD_TIMEOUT,
            method="POST",
        )
    except BodyTooLargeError, TransportError:
        message = "capacity cleanup: provisioning-reload"
        raise SmokeError(message) from None
    require(code == HTTP_OK, "capacity cleanup: provisioning-reload")


def cleanup_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(
            bool(separator) and key.isidentifier(),
            "capacity cleanup: malformed-env-line",
        )
        env[key] = value
    return env


def cleanup(run: Smoke) -> None:
    release = present(run.release, "capacity cleanup: release")
    env = cleanup_env(run.fs.read_text(release / ".env"))
    # The renderer names the immutable directory; mutate only this disposable copy.
    name = Path(env["O11Y_CONFIG_GRAFANA"]).name
    root = release.parent / "config" / name
    provider_path = root / "provisioning/dashboards/agents.yaml"
    provider = yaml.safe_load(run.fs.read_text(provider_path))
    agents = next(p for p in provider["providers"] if p["name"] == "agents")

    def sync() -> None:
        require(
            run.utility(
                ["cp", "-a", "/src/.", run.destination + "/config/"],
                source=release.parent / "config",
            ).code
            == 0,
            "capacity cleanup: config-copy",
        )
        require(
            run.utility(["chmod", "-R", "a+rX", run.destination + "/config"]).code == 0,
            "capacity cleanup: config-permissions",
        )

    def protection(*, value: bool) -> None:
        agents["disableDeletion"] = value
        run.fs.write_atomic(provider_path, yaml.safe_dump(provider), 0o600)
        sync()
        reload(run, "dashboards")

    try:
        protection(value=False)
        run.fs.unlink(root / "dashboards/agents/capacity.json")
        # cp merges directories; remove the runtime file explicitly as well.
        require(
            run.utility(
                [
                    "rm",
                    "-f",
                    run.destination
                    + "/config/"
                    + name
                    + "/dashboards/agents/capacity.json",
                ]
            ).code
            == 0,
            "capacity cleanup: dashboard-remove",
        )
        reload(run, "dashboards")
        require(
            checks.request(run, "/api/dashboards/uid/o11y-agents-capacity")[0]
            == HTTP_NOT_FOUND,
            "capacity cleanup: dashboard-deleted",
        )
    finally:
        failure = sys.exception()
        try:
            protection(value=True)
        except SmokeError, OSError, yaml.YAMLError:
            if failure is None:
                raise
            run.emit("FAIL capacity cleanup: restore-deletion-protection")
    path = root / "provisioning/alerting/rules.yaml"
    rules = yaml.safe_load(run.fs.read_text(path))
    group = next(g for g in rules["groups"] if g["name"] == "agent-capacity")
    removed = tuple(r["uid"] for r in group["rules"])
    rules["deleteRules"] = [{"orgId": 1, "uid": uid} for uid in removed]
    rules["groups"].remove(group)
    remaining = tuple(sorted(r["uid"] for g in rules["groups"] for r in g["rules"]))
    run.fs.write_atomic(path, yaml.safe_dump(rules), 0o600)
    sync()
    reload(run, "alerting")
    preserved(run, removed, remaining)
    restored = yaml.safe_load(run.fs.read_text(provider_path))["providers"]
    require(
        next(p for p in restored if p["name"] == "agents")["disableDeletion"] is True,
        "capacity cleanup: deletion-protection",
    )
    run.emit(
        "ok capacity cleanup: capacity dashboard and five rules "
        "deleted; other rules and Agents dashboards preserved; "
        "deletion protection restored"
    )


def preserved(run: Smoke, removed: tuple[str, ...], remaining: tuple[str, ...]) -> None:
    body = checks.get(run, "/api/v1/provisioning/alert-rules", "capacity cleanup")
    provisioned: list[Document] = json.loads(body)
    require(
        access.identifiers(body, remaining)
        and all(r.get("provenance") == "file" for r in provisioned),
        "capacity cleanup: preserved-rules",
    )
    for uid in removed:
        require(
            checks.request(run, "/api/v1/provisioning/alert-rules/" + uid)[0]
            == HTTP_NOT_FOUND,
            "capacity cleanup: rule-deleted",
        )
    dashboards: list[Document] = json.loads(
        checks.get(run, "/api/search?type=dash-db", "capacity cleanup")
    )
    keep = tuple(uid for uid in access.AGENTS if uid != "o11y-agents-capacity")
    require(
        sorted(d["uid"] for d in dashboards if d.get("folderUid") == access.FOLDER)
        == sorted(keep),
        "capacity cleanup: preserved-dashboard-list",
    )
    for uid in keep:
        meta: Document = decode(
            checks.get(run, "/api/dashboards/uid/" + uid, "capacity cleanup")
        ).get("meta", {})
        require(
            meta.get("provisionedExternalId") == access.DASHBOARD_FILES[uid]
            and meta.get("folderUid") == access.FOLDER,
            "capacity cleanup: preserved-dashboard-provisioning",
        )
