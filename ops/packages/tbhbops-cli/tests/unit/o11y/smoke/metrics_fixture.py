"""Scripted metrics, dashboards, and alert responses for smoke tests."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit
lazy from collections.abc import Callable

import yaml
from tbhbops.o11y import smoke_capacity as capacity
from tbhbops_cli.o11y.smoke import (
    queries,
    runtime,
    transport,
)
from unit.o11y.smoke.grafana_fixture import Http, packed

if TYPE_CHECKING:
    from unit.o11y.smoke.stack_fixture import EngineC

ROOT = next(p for p in Path(__file__).parents if (p / "o11y/compose.yaml").exists())
CONFIG = ROOT / "o11y/config"
lazy from tbhbops.o11y.validate import Document

HEALTH = json.loads(
    (CONFIG / "grafana/dashboards/general/stack-health.json").read_text()
)


DASHBOARD = json.loads((CONFIG / "grafana/dashboards/agents/capacity.json").read_text())


RULES = yaml.safe_load(
    (CONFIG / "grafana/provisioning/alerting/rules.yaml").read_text()
)


def frame(labels: Document, values: list[object]) -> Document:
    return {
        "schema": {
            "fields": [
                {"name": "Time", "type": "time"},
                {"name": "Value", "type": "number", "labels": labels},
            ]
        },
        "data": {"values": [[0], values]},
    }


def vector(values: list[object], labels: list[Document] | None = None) -> bytes:
    return packed(
        {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": (labels or [json.loads("{}")] * len(values))[i],
                        "value": [0, str(v)],
                    }
                    for i, v in enumerate(values)
                ]
            },
        }
    )


def windows() -> list[Document]:
    return [
        {
            "Account": "primary",
            "Vendor": v,
            "Window": w,
            "Utilization": u,
            "Time to reset": t,
            "Projected at reset": p,
        }
        for v, w, u, t, p in (
            ("anthropic", "5h", 0.7, 3600, None),
            ("anthropic", "7d", 0.888, 172800, None),
            ("anthropic", "7d_sonnet", 0, 86400, None),
            ("google", "other", None, None, None),
            ("openai", "5h", 0.25, 7200, None),
        )
    ]


def table_frames(rows: list[Document], refs: str = "ABCD") -> bytes:
    results = json.loads("{}")
    for ref, column in zip(
        "ABCD",
        ("Utilization", "Utilization", "Time to reset", "Projected at reset"),
        strict=True,
    ):
        results[ref] = {
            "status": 200,
            "frames": [
                frame(
                    {
                        "agent_plan_vendor": r["Vendor"],
                        "agent_plan_window": r["Window"],
                        "agent_account": r["Account"],
                        "capacity_key": r["Vendor"]
                        + "|"
                        + r["Window"]
                        + "|"
                        + r["Account"],
                    },
                    [r[column]],
                )
                for r in rows
            ]
            if ref in refs
            else json.loads("[]"),
        }
    return packed({"results": results})


@dataclass
class HttpC(Http):
    engine: EngineC = field(kw_only=True)
    calls: list[tuple[str, str, bytes | None, dict[str, str], int]] = field(
        default_factory=list
    )
    overrides: dict[str, tuple[int, bytes] | Exception] = field(default_factory=dict)
    rate: bool = False
    old: bool = False
    deleted_rules: bool = False
    email_off: bool = False
    dashboard_deleted: bool = False

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: int,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, body, dict(headers), timeout))
        override = self.override_response(url)
        if override is not None:
            return override
        assert timeout in {15, 30}
        path = urlsplit(url).path
        if url.startswith(transport.OTLP):
            if path.endswith("/logs"):
                self.rate = b"o11y-smoke-rate" in (body or b"")
                self.old = self.old or b"o11y-smoke-old" in (body or b"")
            answer: tuple[int, bytes] = self.post(url, body)
        elif "/api/datasources/proxy/" in path:
            answer = self.datasource(method, url, body, headers, timeout)
        elif path == queries.RULES_PATH:
            answer = scheduler_rules()
        elif path.startswith("/api/v1/provisioning/") or path in {
            "/api/v1/eval",
            "/api/ds/query",
        }:
            answer = self.provisioning(method, url, body, headers)
        else:
            answer = self.dashboards(method, url, headers)
        return answer

    def override_response(self, url: str) -> tuple[int, bytes] | None:
        for match, override in self.overrides.items():
            if match in url:
                if isinstance(override, Exception):
                    raise override
                return override
        return None

    def datasource(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: int,
    ) -> tuple[int, bytes]:
        path = urlsplit(url).path
        if path.endswith("/api/v1/query") and "o11y_check" not in url:
            return 200, self.instant(parse_qs(urlsplit(url).query)["query"][0])
        if path.endswith("/api/v1/series"):
            return 200, packed(
                {
                    "data": [
                        {"__name__": "rustfs_start_total"},
                        {"__name__": "target_info"},
                    ]
                }
            )
        if "/uid/tempo/" in path and path.endswith("/api/search"):
            return 200, b'{"traces":[]}'
        if "/uid/loki/" in path and "rustfs" in url:
            return 200, vector([])
        answer: tuple[int, bytes] = self.receipt.request(
            method, url, body, headers, timeout
        )
        return answer

    def dashboards(
        self, method: str, url: str, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        path = urlsplit(url).path
        if path == "/api/dashboards/uid/o11y-health":
            return 200, packed({"dashboard": HEALTH})
        if path == "/api/dashboards/uid/o11y-agents-capacity":
            return (
                (404, b"private")
                if self.dashboard_deleted
                else (200, packed({"dashboard": DASHBOARD}))
            )
        if path == "/api/search" and self.dashboard_deleted:
            return 200, packed(
                [
                    {"uid": uid, "folderUid": "o11y-agents-folder"}
                    for uid in (
                        "o11y-agents",
                        "o11y-agents-claude-code",
                        "o11y-agents-codex",
                        "o11y-agents-runs",
                        "o11y-agents-traces",
                    )
                ]
            )
        answer: tuple[int, bytes] = self.grafana(
            method,
            url.removeprefix(transport.GRAFANA),
            headers.get("Tailscale-User-Login", ""),
        )
        return answer

    def provisioning(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        path = urlsplit(url).path
        if path.startswith("/api/v1/provisioning/alert-rules/"):
            return self.provisioned_rule(path)
        if path == "/api/v1/eval":
            return 200, self.evaluation(json.loads(body or b"{}"))
        if path == "/api/ds/query":
            return 200, self.capacity_panel(json.loads(body or b"{}"))
        if path == "/api/v1/provisioning/contact-points" and self.email_off:
            return 200, packed(
                [
                    {
                        "uid": "o11y-discord",
                        "name": "o11y",
                        "type": "discord",
                        "provenance": "file",
                        "settings": {"url": "[REDACTED]"},
                    }
                ]
            )
        if path == "/api/v1/provisioning/alert-rules" and self.deleted_rules:
            return 200, packed(
                [
                    {"uid": r["uid"], "provenance": "file"}
                    for g in RULES["groups"]
                    if g["name"] != "agent-capacity"
                    for r in g["rules"]
                ]
            )
        answer: tuple[int, bytes] = self.grafana(
            method,
            url.removeprefix(transport.GRAFANA),
            headers.get("Tailscale-User-Login", ""),
        )
        return answer

    def provisioned_rule(self, path: str) -> tuple[int, bytes]:
        uid = path.rsplit("/", 1)[1]
        if self.deleted_rules and uid.startswith("o11y-capacity-"):
            return 404, b"private"
        return 200, packed(
            next(r for g in RULES["groups"] for r in g["rules"] if r["uid"] == uid)
        )

    def instant(self, expression: str) -> bytes:
        monitored = self.monitoring(expression)
        if monitored is not None:
            return monitored
        if expression.startswith("o11y_smoke_"):
            return vector(["5" if "cumulative" in expression else self.engine.delta])
        if expression.startswith("otelcol_exporter_send_failed_log"):
            return vector([1])
        if expression.startswith('{__name__=~"agent_capacity_'):
            return capacity_inventory()
        title = next(
            p["title"]
            for p in HEALTH["panels"]
            for t in p.get("targets", json.loads("[]"))
            if t.get("expr", "").replace("$__rate_interval", "2m") == expression
        )
        if "alloy" in self.engine.stopped:
            return vector(json.loads("[]"))
        return vector(
            [1]
            if title.startswith("Last scrape:")
            else [0]
            if title.startswith("Up:")
            else json.loads("[]")
        )

    def monitoring(self, expression: str) -> bytes | None:
        if expression == "up == 1":
            return vector(
                [1] * 5,
                [
                    {"job": "o11y/" + s, "cluster": "o11y", "namespace": "o11y"}
                    for s in ("alloy", "grafana", "loki", "mimir", "tempo")
                ],
            )
        if expression.startswith("scrape_samples_"):
            return vector([1])
        if expression.startswith("count_over_time(tempo_receiver_refused_spans"):
            return vector([2])
        if expression.startswith("rustfs_start_total"):
            return vector(
                [1],
                [{"instance": "rustfs:9000", "cluster": "o11y", "namespace": "o11y"}],
            )
        if expression.startswith("target_info"):
            return vector(
                [1],
                [{"host_name": "smoke-host", "deployment_environment_name": "smoke"}],
            )
        return None

    def capacity_firing(self, uid: str) -> tuple[str, list[str]]:
        label = (
            "agent_plan_vendor" if uid == "o11y-capacity-stale" else "agent_plan_window"
        )
        if not self.engine.reset_uploaded:
            return label, []
        return label, {
            "warn": ["5h", "7d"],
            "shed": ["7d"],
            "stop": [],
            "exhaustion": [],
            "stale": ["google"],
        }[uid.removeprefix("o11y-capacity-")]

    def evaluation(self, prepared: Document) -> bytes:
        expr = prepared["data"][0]["model"]["expr"]
        rule, threshold = evaluation_rule(prepared, expr)
        uid = rule["uid"]
        firing = json.loads("[]")
        count = 1
        label = "job"
        if uid.startswith("o11y-capacity-"):
            label, firing = self.capacity_firing(uid)
        elif uid == "o11y-service-down":
            count = 6
            firing = (
                ["o11y/tempo"] if "tempo" in self.engine.stopped else json.loads("[]")
            )
        elif uid == "o11y-disk-low" or (uid == "o11y-rustfs-memory" and threshold == 0):
            firing = ["o11y/rustfs"]
        elif uid == "o11y-ingest-dropped" and self.old:
            firing = ["o11y/loki"]
        elif uid == "o11y-log-rate" and threshold == 0 and self.rate:
            firing = [None]
            label = "service_namespace"
        frames = [
            frame(
                ({label: v} if v is not None else json.loads("{}"))
                | (
                    {"reason": "greater_than_max_sample_age"}
                    if uid == "o11y-ingest-dropped"
                    else json.loads("{}")
                ),
                [1],
            )
            for v in firing
        ]
        frames.extend(
            frame(json.loads("{}"), [0]) for _ in range(max(0, count - len(frames)))
        )
        return packed(
            {"results": {prepared["condition"]: {"status": 200, "frames": frames}}}
        )

    def capacity_panel(self, request: Document) -> bytes:
        title = next(
            p["title"]
            for p in DASHBOARD["panels"]
            if p.get("targets")
            and p["targets"][0]["expr"] == request["queries"][0]["expr"]
        )
        if title == "Live windows":
            return self.live_capacity_table(request)
        if title == "Freshness and source":
            rows = [
                frame(
                    {
                        "agent_plan_window": "5h",
                        "agent_account": "primary",
                        "agent_capacity_freshness": f,
                        "agent_capacity_source": s,
                    },
                    [0],
                )
                for f, s in (
                    ("fresh", "claude_statusline"),
                    ("carried", "claude_statusline"),
                    ("carried", "codex_rollout"),
                    ("fresh", "codex_rollout"),
                    ("fresh", "claude_statusline"),
                )
            ]
        elif (
            title
            == "Projected utilization at reset (3 h or 12 h of data, capped at 100%)"
        ):
            rows = [frame({"agent_plan_window": "7d"}, [None])]
        elif title == "Utilization history by account and window":
            rows = [
                frame({"agent_plan_vendor": vendor, "agent_account": "primary"}, [1])
                for vendor in ("anthropic", "openai")
            ]
        else:
            rows = [
                frame(
                    {"agent_plan_vendor": "anthropic", "agent_account": "primary"},
                    [1],
                )
            ]
        return packed({"results": {"A": {"status": 200, "frames": rows}}})

    def live_capacity_table(self, request: Document) -> bytes:
        rows = windows()
        now = int(request["to"]) // 1000
        if now >= 1000 + 8 * 86400:
            rows = json.loads("[]")
        elif now >= 2200:
            for row in rows:
                for column in capacity.NUMERIC_COLUMNS:
                    row[column] = None
        elif not self.engine.reset_uploaded:
            for row in rows:
                row["Time to reset"] = None
        return table_frames(rows)


def scheduler_rules() -> tuple[int, bytes]:
    return (
        200,
        packed(
            {
                "data": {
                    "groups": [
                        {
                            "name": g["name"],
                            "rules": [
                                {
                                    "name": r["title"],
                                    "health": "ok",
                                    "state": "inactive",
                                }
                                for r in g["rules"]
                            ],
                        }
                        for g in RULES["groups"]
                    ]
                }
            }
        ),
    )


def capacity_inventory() -> bytes:
    labels = [
        {
            "__name__": name,
            "agent_plan_vendor": vendor,
            "host_name": "capacity-fixture-a",
            "agent_plan_window": window,
            "agent_account": "primary",
            "agent_capacity_source": "claude_statusline",
            "agent_capacity_freshness": "carried",
        }
        for name in (capacity.UTIL, capacity.RESET, capacity.AGE)
        for vendor, window in (
            ("anthropic", "5h"),
            ("anthropic", "7d"),
            ("anthropic", "7d_sonnet"),
            ("openai", "5h"),
            ("anthropic", "5h"),
        )
    ]
    return vector([1] * len(labels), labels)


def evaluation_rule(prepared: Document, expr: str) -> tuple[Document, int]:
    rule = next(
        r
        for g in RULES["groups"]
        for r in g["rules"]
        if r["data"][0]["model"]["expr"] == expr
    )
    threshold = prepared["data"][-1]["model"]["conditions"][0]["evaluator"]["params"][0]
    if rule["uid"] in {
        "o11y-capacity-warn",
        "o11y-capacity-shed",
        "o11y-capacity-stop",
    }:
        rule = next(
            r
            for g in RULES["groups"]
            for r in g["rules"]
            if r["data"][0]["model"]["expr"] == expr
            and r["data"][-1]["model"]["conditions"][0]["evaluator"]["params"][0]
            == threshold
        )
    return rule, threshold


def alert_evaluation(
    context: tuple[str, HttpC],
    original: Callable[..., list[Document] | None],
    run: runtime.Smoke,
    uid: str,
    threshold: int | None = None,
) -> list[Document] | None:
    mode, http = context
    rows = original(run, uid, threshold)
    if mode == "series" and uid == "o11y-rustfs-memory" and threshold is None:
        return []
    if mode == "ingest" and uid == "o11y-ingest-dropped" and http.old:
        return [{"labels": {"job": "o11y/loki", "reason": "wrong"}, "values": [1]}]
    if mode == "isolated" and uid == "o11y-alloy-export" and http.old:
        return [{"labels": {"component_id": "fixture"}, "values": [1]}]
    if uid == "o11y-log-rate" and http.rate:
        if mode == "quiet-rate" and threshold is None:
            return []
        if mode == "positive-rate" and threshold == 0:
            return [{"labels": json.loads("{}"), "values": [0]}]
    return rows
