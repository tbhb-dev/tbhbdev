"""Slice C with fake processes, HTTP, files, entropy and time."""

# pylint: disable=duplicate-code  # verify protocol literals independently

import hashlib
import io
import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
lazy from collections.abc import Callable, Mapping, Sequence

import pytest
import yaml
from tbhbops_cli.o11y import smoke as command
from tbhbops_cli.o11y import smoke_capacity as cap
from tbhbops_cli.o11y import smoke_checks as checks
from tbhbops_cli.o11y import smoke_lifecycle as lifecycle
from tbhbops_cli.o11y import smoke_stages as stages
from tbhbops_cli.o11y.http import BodyTooLargeError, LocalAdminHttp, TransportError
from tbhbops_cli.o11y.runner import Result

from tbhbops.o11y import init, smoke
from tbhbops.o11y import smoke_capacity as capacity
lazy from tbhbops.o11y.validate import Document

from .test_o11y_admin_http import Response, connection  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace
from .test_o11y_smoke import OUT  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace
from .test_o11y_smoke_b import EngineB, Http, instance_b, packed  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace
lazy from .test_o11y_cli import Fake  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace

ROOT = next(p for p in Path(__file__).parents if (p / "o11y/compose.yaml").exists())
SOURCE = Path("/stack")
CONFIG = ROOT / "o11y/config"
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
class EngineC(EngineB):
    calls: list[tuple[list[str], dict[str, str], int]] = field(default_factory=list)
    overrides: dict[str, Result] = field(default_factory=dict)
    upload: str = "{}\n200"

    states: dict[str, tuple[str, str]] = field(
        default_factory=lambda: {s: (s, "2026-10-25T05:38:26Z") for s in smoke.SERVICES}
    )
    stopped: set[str] = field(default_factory=set)
    owner_time: int = 0
    reset_uploaded: bool = False
    uploaded: list[str] = field(default_factory=list)
    delta: str = "7"
    token: str = ""
    removed: bool = False

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        if any(match in " ".join(argv) for match in self.overrides):
            result: Result = super().run(argv, env, timeout, input_data=input_data)
            return result
        if argv[1] in {"top", "stop", "start", "logs", "ps"}:
            self.calls.append((list(argv), dict(env), timeout))
            return Result(
                0,
                "abcdef012345"
                if argv[1] == "ps"
                else "process arguments"
                if argv[1] == "top"
                else "owner org Admin reconciled",
            )
        if input_data is not None:
            self.uploaded.append(input_data)
            data = json.loads(input_data)
            if (
                "resourceMetrics" in data
                and data["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["name"]
                == capacity.RESET
            ):
                self.reset_uploaded = True
        if (
            "--entrypoint" in argv
            and argv[argv.index("--entrypoint") + 1] == "rm"
            and "-f" in argv
        ):
            self.calls.append((list(argv), dict(env), timeout))
            self.removed = True
            return Result(0)
        result: Result = super().run(argv, env, timeout, input_data=input_data)
        return result

    def inspect(self, argv: Sequence[str]) -> Result:
        template, name = argv[-2:]
        if template == "{{.Id}} {{.State.StartedAt}}":
            return Result(0, " ".join(self.states[name]))
        if template == "{{.State.StartedAt}}":
            return Result(0, str(self.owner_time))
        if template == "{{json .Args}}":
            return Result(0, '["run","--stability.level=experimental"]')
        if template == "{{.State.Status}}:{{.State.ExitCode}}":
            return Result(0, "exited:0")
        result: Result = super().inspect(argv)
        return result

    def compose(self, argv: Sequence[str], env: Mapping[str, str]) -> Result:
        result = None
        if "up" in argv and argv[-2:] == ["up", "-d"]:
            result = self.release_up(argv)
        elif "stop" in argv:
            self.stopped.add(argv[-1])
            result = Result(0)
        elif "start" in argv:
            self.stopped.discard(argv[-1])
            result = Result(0)
        elif "restart" in argv:
            self.delta = "2"
            result = Result(0)
        elif "run" in argv and "grafana-owner" in argv:
            result = Result(0, "abcdef012345" + "6" * 52)
        elif "top" in argv:
            result = Result(0, "process arguments")
        elif "logs" in argv and argv[-1] == "--no-color":
            result = Result(0, "grafana-owner org Admin reconciled")
        elif "exec" in argv:
            result = self.notifications(argv)
        elif "logs" in argv and argv[-1] == "rustfs":
            result = Result(0, 'Starting: rustfs\n{"console_enabled":false}\n')
        if result is None:
            inherited: Result = super().compose(argv, env)
            result = inherited
        return result

    def release_up(self, argv: Sequence[str]) -> Result:
        self.owner_time += 1
        if "/output/release/compose.yaml" in argv:
            self.states.update(
                {
                    "loki": ("new-loki", "2026-10-25T05:40:05Z"),
                    "alloy": ("alloy", "2026-10-25T05:40:07Z"),
                    "grafana": ("grafana", "2026-10-25T05:40:08Z"),
                }
            )
        return Result(0, "started")

    def notifications(self, argv: Sequence[str]) -> Result | None:
        if argv[-1] == checks.WEBHOOK_POSTS:
            return Result(
                0,
                "path=/12345/"
                + self.token
                + '\n{"content":"[FIRING:1] Disk free below the '
                'reserve","embeds":[{}]}',
            )
        if argv[-1].endswith("/messages"):
            return Result(
                0,
                json.dumps(
                    {
                        "messages": [
                            {
                                "To": [{"Address": "smoke-to@example.invalid"}],
                                "From": {"Address": "smoke-from@example.invalid"},
                                "Subject": "[FIRING:1] Disk free below the reserve",
                            }
                        ]
                    }
                ),
            )
        return None

    def inside_response(self, argv: Sequence[str]) -> Result | None:
        if argv[-1] == "http://alloy:4318/v1/metrics":
            return Result(0, self.upload)
        if argv[-1] == "http://mimir:8080/metrics":
            return Result(0, "# TYPE cortex_build_info gauge\n\n200")
        result: Result | None = super().inside_response(argv)
        return result


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
        if url.startswith(stages.OTLP):
            if path.endswith("/logs"):
                self.rate = b"o11y-smoke-rate" in (body or b"")
                self.old = self.old or b"o11y-smoke-old" in (body or b"")
            answer: tuple[int, bytes] = self.post(url, body)
        elif "/api/datasources/proxy/" in path:
            answer = self.datasource(method, url, body, headers, timeout)
        elif path == checks.RULES_PATH:
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
            url.removeprefix(stages.GRAFANA),
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
            url.removeprefix(stages.GRAFANA),
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


@dataclass
class AdminC:
    http: HttpC
    calls: list[tuple[str, bytes, int, str]] = field(default_factory=list)
    answer: tuple[int, bytes] | Exception | None = None
    protections: list[bool] = field(default_factory=list)

    def request(
        self, url: str, password: bytes, timeout: int, *, method: str
    ) -> tuple[int, bytes]:
        self.calls.append((url, password, timeout, method))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is not None:
            return self.answer
        if "alerting/reload" in url:
            self.http.deleted_rules = True
        if "dashboards/reload" in url:
            fs: Fake = self.http.engine.fs
            path = next(
                p
                for p in fs.files
                if OUT / "email-disabled/config" in p.parents
                and p.name == "agents.yaml"
            )
            enabled = yaml.safe_load(fs.read_text(path))["providers"][0][
                "disableDeletion"
            ]
            self.protections.append(enabled)
            if enabled is False and self.http.engine.removed:
                self.http.dashboard_deleted = True
        return 200, b"{}"


def instance_c() -> tuple[Fake, EngineC, HttpC, command.Smoke]:
    fs, _, _, run = instance_b()
    for real in sorted((ROOT / "o11y").rglob("*")):
        if real.is_file() and real.name != "AGENTS.md":
            target = SOURCE / real.relative_to(ROOT / "o11y")
            for parent in reversed(target.parents):
                fs.mkdir(parent)
            fs.write_atomic(target, real.read_bytes(), 0o600)
    engine = EngineC(fs)
    engine.token = run.sentinels.token.decode()
    http = HttpC(engine=engine)
    admin = AdminC(http)
    run.system = replace(run.system, runner=engine, receipt_http=http, admin_http=admin)

    def random(size: int) -> bytes:
        return b"\x01" * size

    run.system = replace(run.system, random_bytes=random)
    return fs, engine, http, run


def test_capacity_smoke_upload_phases_and_panels() -> None:
    fs, engine, _http, run = instance_c()
    cap.smoke(run, SOURCE)
    assert len(engine.uploaded) == 2
    history = json.loads(fs.read_text(OUT / "capacity.json"))
    before, after = map(json.loads, engine.uploaded)
    for original, left, right in zip(
        history["resourceMetrics"],
        before["resourceMetrics"],
        after["resourceMetrics"],
        strict=True,
    ):

        def all_metrics(r: Document) -> list[Document]:
            metrics: list[Document] = r["scopeMetrics"][0]["metrics"]
            return metrics

        assert all(m["name"] != capacity.RESET for m in all_metrics(left))
        assert all(m["name"] == capacity.RESET for m in all_metrics(right))
        assert sorted(all_metrics(original), key=lambda m: m["name"]) == sorted(
            all_metrics(left) + all_metrics(right), key=lambda m: m["name"]
        )
    for argv, _, timeout in engine.calls:
        if "curl" in argv:
            assert argv[argv.index("-m") + 1] == "30"
            assert timeout == 35
            assert argv[-3:] == ["--data-binary", "@-", "http://alloy:4318/v1/metrics"]
    assert run.transcript[-1].startswith("ok capacity:")


def test_group7_self_monitoring() -> None:
    _, engine, _, run = instance_c()
    checks.group7(run, SOURCE)
    assert engine.stopped == set()
    assert run.transcript[-1].startswith("ok 7:")


def test_release_recreation_and_direct_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, _http, run = instance_c()
    lifecycle.group8(run, SOURCE)
    assert run.release == OUT / "release"
    assert fs.read_bytes(OUT / "src/config/loki/config.yaml").endswith(b"\n#\n")

    def receipt(*_args: object, **kwargs: object) -> tuple[list[str], bool]:
        assert kwargs == {"signals": "traces,logs,metrics", "safe_failures": True}
        return ["ok fixture receipt"], True

    monkeypatch.setattr(checks.receipt, "receipt", receipt)
    lifecycle.group9(run)
    assert "ok fixture receipt\n" in run.transcript
    direct = [argv for argv, _, _ in engine.calls if argv[1] in {"stop", "start"}]
    assert len(direct) == 2
    assert len(direct[0]) == 8
    assert set(direct[0][2:]) == set(smoke.SERVICES)


def test_sentinel_search_and_transcript_refresh() -> None:
    fs, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    lifecycle.group10(run)
    assert run.transcript[-1].startswith("ok 10:")
    assert run.finalized is False
    run.finalize_transcript()
    assert fs.read_text(OUT / "transcript.log") == "".join(run.transcript)
    assert len([a for a, _, _ in engine.calls if "top" in a]) == 7


@pytest.mark.parametrize(
    "place",
    [
        "release/.env",
        "again/release/.env",
        "src/config/loki/config.yaml",
        "config/fixture",
        "containers.log",
        "owner-repeated.log",
        "processes.log",
        "model.log",
        "transcript.log",
    ],
)
def test_group10_contamination_never_prints_value(
    place: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, _engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    original = command.scan_path
    needle = run.sentinels.scan_values()[1]

    def scan(filesystem: Fake, path: Path, needles: tuple[bytes, ...]) -> None:
        target = OUT / place
        if path == target or path in target.parents:
            fs.write_atomic(target, b"prefix\xff" + needle, 0o600)
        original(filesystem, path, needles)

    monkeypatch.setattr(lifecycle, "scan_path", scan)
    with pytest.raises(command.SmokeError, match=r"sentinel secret") as failure:
        lifecycle.group10(run)
    assert needle.decode() not in str(failure.value)
    assert needle.decode() not in "".join(run.transcript)


def test_delta_and_alert_delivery() -> None:
    _, engine, http, run = instance_c()
    checks.group11(run)
    assert engine.delta == "2"
    checks.group12(run, SOURCE)
    assert http.old
    assert http.rate
    assert run.transcript[-1].startswith("ok 12:")
    assert not any("smoke diagnostics:" in line for line in run.transcript)


def test_email_transition_and_capacity_cleanup() -> None:
    _fs, engine, http, run = instance_c()
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    cap.cleanup(run)
    assert engine.removed
    assert run.transcript[-1].startswith("ok capacity cleanup:")
    admin = run.system.admin_http
    assert isinstance(admin, AdminC)
    assert len(admin.calls) == 4
    assert admin.protections == [False, False, True]
    assert all(
        method == "POST" and timeout == 30 and "/api/admin/provisioning/" in url
        for url, _, timeout, method in admin.calls
    )
    assert all(
        password == dict(run.sentinels.values)["grafana_admin_password"]
        for _, password, _, _ in admin.calls
    )
    assert "grafana_admin_password" not in str(engine.calls)


@pytest.mark.parametrize(
    "answer", [(500, b"private"), TransportError("private"), BodyTooLargeError()]
)
def test_reload_failure_restores_protection(
    answer: tuple[int, bytes] | Exception,
) -> None:
    fs, _, http, run = instance_c()
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    assert isinstance(run.system.admin_http, AdminC)
    run.system.admin_http.answer = answer
    with pytest.raises(
        command.SmokeError, match=r"^capacity cleanup: provisioning-reload$"
    ):
        cap.cleanup(run)
    provider = next(
        p
        for p in fs.files
        if OUT / "email-disabled/config" in p.parents and p.name == "agents.yaml"
    )
    assert (
        yaml.safe_load(fs.read_text(provider))["providers"][0]["disableDeletion"]
        is True
    )


def test_admin_post_method(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = connection(monkeypatch, Response(b"ok"))
    assert LocalAdminHttp().request(
        "http://localhost:123/reload", b"fixture", 30, method="POST"
    ) == (201, b"ok")
    assert constructor.return_value.request.call_args.args == ("POST", "/reload")


def stage_run(stage: str) -> tuple[Fake, EngineC, HttpC, command.Smoke]:
    fs, engine, http, run = instance_c()
    if stage == "group10":
        lifecycle.group8(run, SOURCE)
    if stage in {"group13", "cleanup"}:
        http.email_off = True
    if stage == "cleanup":
        lifecycle.group13(run, SOURCE)
    return fs, engine, http, run


STAGES: dict[str, Callable[..., None]] = {
    "smoke": cap.smoke,
    "group7": checks.group7,
    "group8": lifecycle.group8,
    "group9": lifecycle.group9,
    "group10": lifecycle.group10,
    "group11": checks.group11,
    "group12": checks.group12,
    "group13": lifecycle.group13,
    "cleanup": cap.cleanup,
}


def invoke(stage: str, run: command.Smoke) -> None:
    if stage in {"smoke", "group7", "group8", "group12", "group13"}:
        STAGES[stage](run, SOURCE)
    else:
        STAGES[stage](run)


def stage_contract(stage: str) -> Document:
    fs, engine, http, run = stage_run(stage)
    count = len(run.transcript)
    engine.calls.clear()
    http.calls.clear()
    fs.calls.clear()
    invoke(stage, run)
    assert isinstance(run.system.admin_http, AdminC)
    return {
        "commands": [[argv, timeout] for argv, _, timeout in engine.calls],
        "http": [
            [method, url, body.decode() if body is not None else None, headers, timeout]
            for method, url, body, headers, timeout in http.calls
        ],
        "uploads": engine.uploaded,
        "transcript": run.transcript[count:],
        "admin": [
            [url, timeout, method]
            for url, _, timeout, method in run.system.admin_http.calls
        ],
        "writes": {
            str(p): [mode, hashlib.sha256(fs.read_bytes(p)).hexdigest()]
            for op, p, mode in (call for call in fs.calls if call[0] == "write")
            if p in fs.files
        },
        "files": {
            str(p.relative_to(OUT)): fs.read_text(p)
            for p in sorted(fs.files)
            if OUT in p.parents
            and p.name
            in {"agents.yaml", "rules.yaml", "processes.log", "transcript.log"}
        },
    }


@pytest.mark.parametrize("stage", STAGES)
def test_stage_protocol_contract(stage: str) -> None:
    expected = json.loads(
        (Path(__file__).parent / "testdata" / f"smoke-c-{stage}.json").read_text()
    )
    assert stage_contract(stage) == expected


@pytest.mark.parametrize(
    "answer", [(500, b"private"), TransportError("private"), BodyTooLargeError()]
)
def test_query_failure_boundaries(answer: tuple[int, bytes] | Exception) -> None:
    _, _, http, run = instance_c()
    http.overrides["/query"] = answer
    assert checks.request(run, "/query") == (
        (0, b"") if isinstance(answer, Exception) else answer
    )
    with pytest.raises(command.SmokeError, match=r"^fixture: query-http$"):
        checks.get(run, "/query", "fixture")
    http.overrides = {"/api/v1/query": answer}
    assert checks.mimir(run, "test", time=123) == b""
    assert http.calls[-1][1].endswith("query=test&time=123")
    http.overrides = {"/alert-rules/": answer}
    assert checks.evaluate(run, "o11y-disk-low") is None
    http.overrides = {"/api/dashboards/": answer}
    assert checks.panel(run, "test") is None
    assert cap.panel(run, "Live windows", 1000) is None


def test_invalid_eval_and_panel_data() -> None:
    _, _, http, run = instance_c()
    http.overrides["/alert-rules/"] = (200, b"null")
    assert checks.evaluate(run, "o11y-disk-low") is None
    http.overrides = {"/api/v1/eval": (500, b"private")}
    assert checks.evaluate(run, "o11y-disk-low") is None
    http.overrides = {"/api/dashboards/": (200, b"null")}
    assert checks.panel(run, "test") is None
    assert cap.panel(run, "Live windows", 1000) is None
    http.overrides = {"/api/ds/query": (500, b"private")}
    assert cap.panel(run, "Live windows", 1000) is None


@pytest.mark.parametrize(
    ("values", "fresh", "stale"),
    [
        (json.loads("[]"), False, True),
        (["89"], True, False),
        (["90"], False, True),
        (["91"], False, True),
        (None, False, False),
        (["bad"], False, False),
        ([None], False, False),
        (["1", "2"], False, False),
    ],
)
def test_panel_freshness_boundary(
    values: list[str] | None, fresh: bool, stale: bool
) -> None:
    assert checks.recent(values) is fresh
    assert checks.recent(values, stale=True) is stale


@pytest.mark.parametrize("value", ["bad", None])
def test_monitoring_invalid_numbers(value: str) -> None:
    _, _, http, run = instance_c()
    http.overrides["/api/v1/query?"] = (
        200,
        packed({"status": "success", "data": {"result": [{"value": [0, value]}]}}),
    )
    with pytest.raises(command.SmokeError, match=r"^7: scrape-jobs-up$"):
        checks.group7(run, SOURCE)


def test_refused_counter_malformed_and_rustfs_series_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, http, run = instance_c()
    original = http.instant

    def instant(expr: str) -> bytes:
        return (
            vector(["bad"])
            if expr.startswith("count_over_time(tempo")
            else original(expr)
        )

    monkeypatch.setattr(http, "instant", instant)
    with pytest.raises(command.SmokeError, match=r"^7: refused-counter-samples$"):
        checks.group7(run, SOURCE)
    http.overrides["/api/v1/series?"] = (200, b"null")
    with pytest.raises(command.SmokeError, match=r"^7: rustfs-metric-allowlist$"):
        checks.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))


@pytest.mark.parametrize(
    "title",
    [
        "Freshness and source",
        "Projected utilization at reset (3 h or 12 h of data, capped at 100%)",
        "Utilization history by account and window",
        "Claude Code tokens per second (fleet total)",
        "Claude Code cost per hour (fleet total)",
        "Codex tokens per second",
    ],
)
def test_capacity_panel_values_reject_missing_and_bad(title: str) -> None:
    for rows in (
        None,
        json.loads("[]"),
        [json.loads("{}")],
        [{"labels": json.loads("{}"), "values": json.loads("[]")}],
        [{"labels": json.loads("{}"), "values": [None]}],
    ):
        assert not cap.panel_ok(title, rows)
    if title == "Codex tokens per second":
        for value in (0.999999, 1.000001, 0, 2):
            assert not cap.panel_ok(title, [{"labels": {}, "values": [value]}])
    if title.startswith("Claude"):
        assert not cap.panel_ok(title, [{"labels": {}, "values": [0]}])


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([None, 0.25, 0.30], True),
        ([None, None], False),
        ([1], True),
        ([0.25], True),
        ([0], True),
        ([0.25, 1.01], False),
        ([-0.01, 0.25], False),
        ([float("nan")], False),
    ],
)
def test_capacity_history_numeric_samples(
    values: list[float | None], *, expected: bool
) -> None:
    assert (
        cap.panel_ok(
            "Utilization history by account and window",
            [{"labels": {}, "values": values}],
        )
        is expected
    )


def test_capacity_history_requires_numeric_sample_in_every_series() -> None:
    assert not cap.panel_ok(
        "Utilization history by account and window",
        [{"values": [0.25]}, {"values": [None, None]}],
    )


def test_capacity_readiness_retries_every_countdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, engine, _, run = instance_c()
    original = cap.panel
    attempts = json.loads("[]")

    def delayed(run: command.Smoke, title: str, now: int) -> list[Document] | None:
        rows = original(run, title, now)
        if title == "Live windows" and now == 1000 and engine.reset_uploaded:
            assert rows is not None
            attempts.append(now)
            if len(attempts) == 1:
                for row in rows:
                    row["Time to reset"] = None
            elif len(attempts) == 2:
                rows[-1]["Time to reset"] = None
        return rows

    monkeypatch.setattr(cap, "panel", delayed)
    cap.smoke(run, SOURCE)
    assert attempts == [1000, 1000, 1000]
    assert run.system.monotonic() == 4


def test_safe_receipt_failures_preserve_helper_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, http, run = instance_c()
    http.overrides["/v1/traces"] = (500, b"private")
    with pytest.raises(command.SmokeError, match=r"^7: receipt$"):
        checks.helper_receipt(run, "7", traces=True)
    assert run.transcript[-1] == "FAIL receipt: signal-check\n"
    http.overrides.clear()
    http.overrides["/uid/loki/"] = (200, b'{"status":"success","data":{"result":[]}}')
    with pytest.raises(command.SmokeError, match=r"^9: receipt$"):
        checks.helper_receipt(run, "9")
    assert run.transcript[-1] == "FAIL receipt: signal-check\n"
    assert not any(line.startswith("FAIL receipt Loki:") for line in run.transcript)

    def denied(*_args: object, **_kwargs: object) -> None:
        message = "private"
        raise OSError(message)

    monkeypatch.setattr(run.fs, "read_text", denied)
    lines, passed = checks.receipt.receipt(
        run.system, None, stages.GRAFANA, recheck=OUT / "receipt", safe_failures=True
    )
    assert not passed
    assert lines == ["FAIL receipt: operation-unavailable"]


def test_flow_order_and_stop_after_five(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, run = instance_c()
    events: list[str] = json.loads("[]")

    def recorder(name: str) -> Callable[..., None]:
        def record(*_args: object) -> None:
            if name == "prepare":
                assert _args == (SOURCE,)
            elif name in {
                "group1",
                "group2",
                "group3",
                "group3a",
                "finalize_transcript",
                "scans",
            }:
                assert _args == ()
            elif name in {"smoke", "group7", "group8", "group12", "group13"}:
                assert _args == (run, SOURCE)
            else:
                assert _args == (run,)
            events.append(name)

        return record

    for name in (
        "prepare",
        "group1",
        "group2",
        "group3",
        "group3a",
        "finalize_transcript",
        "scans",
    ):
        monkeypatch.setattr(run, name, recorder(name))
    for module, names in (
        (stages, ("group4", "group5", "export_persistence", "group6")),
        (checks, ("group7", "group11", "group12")),
        (lifecycle, ("group8", "group9", "group10", "group13")),
        (cap, ("smoke", "cleanup")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, recorder(name))
    execute_stages: Callable[[command.Smoke, Path], None] = vars(command)["_execute"]
    execute_stages(run, SOURCE)
    assert events == [
        "prepare",
        "group1",
        "group2",
        "group3",
        "group3a",
        "group4",
        "group5",
        "export_persistence",
        "smoke",
        "group6",
        "group7",
        "group8",
        "group9",
        "group10",
        "group11",
        "group12",
        "group13",
        "cleanup",
        "finalize_transcript",
        "scans",
    ]
    events.clear()
    run.stop_after = 5
    execute_stages: Callable[[command.Smoke, Path], None] = vars(command)["_execute"]
    execute_stages(run, SOURCE)
    assert events == [
        "prepare",
        "group1",
        "group2",
        "group3",
        "group3a",
        "group4",
        "group5",
        "finalize_transcript",
        "scans",
    ]


def fault_cases(kind: str) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for stage in STAGES:
        path = Path(__file__).parent / "testdata" / f"smoke-c-{stage}.json"
        if path.exists():
            saved = json.loads(path.read_text())
            texts: set[str] = {
                " ".join(call[0]) if kind == "commands" else call[1]
                for call in saved[kind]
            }
            cases.extend((stage, text) for text in sorted(texts))
    return cases


@pytest.mark.parametrize(("stage", "match"), fault_cases("commands"))
def test_every_process_failure_names_a_rule(stage: str, match: str) -> None:
    _, engine, _, run = stage_run(stage)
    engine.overrides[match] = Result(17, "private-body", "private-error")
    with pytest.raises(command.SmokeError) as failure:
        invoke(stage, run)
    assert "private" not in str(failure.value)
    failures = json.loads(
        (Path(__file__).parent / "testdata/smoke-c-failures.json").read_text()
    )
    assert str(failure.value) == failures["commands|" + stage + "|" + match]
    assert "private" not in "".join(run.transcript)


@pytest.mark.parametrize(("stage", "match"), fault_cases("http"))
def test_every_http_failure_names_a_rule(stage: str, match: str) -> None:
    _, _, http, run = stage_run(stage)
    http.overrides[match] = (500, b"private-body")
    with pytest.raises(command.SmokeError) as failure:
        invoke(stage, run)
    assert "private" not in str(failure.value)
    failures = json.loads(
        (Path(__file__).parent / "testdata/smoke-c-failures.json").read_text()
    )
    assert str(failure.value) == failures["http|" + stage + "|" + match]
    assert "private" not in "".join(run.transcript)


@pytest.mark.parametrize(
    ("response", "rule"),
    [
        ("200", "upload-http"),
        ("{}\n500", "upload-http"),
        ("{}\n200X", "upload-http"),
        ("{}\n200 ", "upload-http"),
        ('{"partialSuccess":{"rejectedDataPoints":1}}\n200', "rejected-points"),
        ("private\n200", "rejected-points"),
    ],
)
def test_capacity_upload_failures(response: str, rule: str) -> None:
    _, engine, _, run = instance_c()
    engine.upload = response
    with pytest.raises(command.SmokeError, match="^capacity: " + rule + "$"):
        cap.upload(run, b"{}", "fixture.json")


def test_capacity_multiline_upload_and_newline_status() -> None:
    fs, engine, _, run = instance_c()
    engine.upload = '{\n"partialSuccess":{}\n}\n200\n'
    cap.upload(run, b"{}", "fixture.json")
    assert fs.read_text(OUT / "fixture.json") == "{}"
    assert ("write", OUT / "fixture.json", 0o600) in fs.calls


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("health", "empty-rules-normal"),
        ("joined", "joined-live-windows"),
        ("inventory", "inventory-numeric-columns"),
        ("expired", "expired-inventory"),
        ("metadata", "panel-values"),
    ],
)
def test_capacity_semantic_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, http, run = instance_c()
    if mode == "health":
        http.overrides[checks.RULES_PATH] = (200, b"{}")
    else:
        original = cap.panel

        def panel(run: command.Smoke, title: str, now: int) -> list[Document] | None:
            rows = original(run, title, now)
            if mode == "joined" and title == "Live windows" and engine.reset_uploaded:
                return []
            if mode == "inventory" and now == 2200:
                return windows()
            if mode == "expired" and now > 2200:
                return windows()
            if mode == "metadata" and title == "Freshness and source":
                return []
            return rows

        monkeypatch.setattr(cap, "panel", panel)
    with pytest.raises(command.SmokeError, match="^capacity: " + rule + "$"):
        cap.smoke(run, SOURCE)


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("health", "12: rule-health"),
        ("series", "12: baseline-series-count"),
        ("ingest", "12: ingest-discard-reason"),
        ("isolated", "12: isolated-export-loss"),
        ("quiet-rate", "12: quiet-log-rate"),
        ("positive-rate", "12: positive-log-rate"),
    ],
)
def test_alert_semantic_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, http, run = instance_c()
    original = checks.evaluate

    monkeypatch.setattr(
        checks, "evaluate", partial(alert_evaluation, (mode, http), original)
    )
    if mode == "health":
        http.overrides[checks.RULES_PATH] = (200, b"{}")
    with pytest.raises(command.SmokeError, match="^" + rule + "$"):
        checks.group12(run, SOURCE)


class PastRuleHealthError(Exception):
    pass


@pytest.mark.parametrize("unhealthy_reads", [0, 3])
def test_rule_health_retries_until_healthy(
    unhealthy_reads: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, run = instance_c()
    reads = 0

    def get(_run: command.Smoke, path: str, group: str) -> bytes:
        nonlocal reads
        assert (path, group) == (checks.RULES_PATH, "12")
        reads += 1
        return b"{}" if reads <= unhealthy_reads else scheduler_rules()[1]

    def evaluate(*_args: object) -> None:
        raise PastRuleHealthError

    monkeypatch.setattr(checks, "get", get)
    monkeypatch.setattr(checks, "evaluate", evaluate)
    with pytest.raises(PastRuleHealthError):
        checks.group12(run, SOURCE)
    assert reads == unhealthy_reads + 1
    assert run.system.monotonic() == 2 * unhealthy_reads


def test_rule_health_gives_up_after_timeout() -> None:
    _, _, http, run = instance_c()
    http.overrides[checks.RULES_PATH] = (200, b"{}")
    with pytest.raises(command.SmokeError, match=r"^12: rule-health$"):
        checks.group12(run, SOURCE)
    assert run.system.monotonic() == checks.RULE_TIMEOUT


def test_rule_health_http_failure_is_not_retried() -> None:
    _, _, http, run = instance_c()
    http.overrides[checks.RULES_PATH] = (503, b"")
    with pytest.raises(command.SmokeError, match=r"^12: query-http$"):
        checks.group12(run, SOURCE)
    assert run.system.monotonic() == 0


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("stability", "alloy-stability"),
        ("values", "delta-cumulative-values"),
        ("discards", "no-parse-discards"),
        ("restarted-discards", "restarted-no-parse-discards"),
        ("otlp", "otlp-listening"),
        ("point", "restarted-point-upload"),
        ("restarted-value", "restarted-delta-value"),
    ],
)
def test_delta_semantic_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, http, run = instance_c()
    if mode == "stability":
        engine.overrides["{{json .Args}}"] = Result(0, "[]")
    if mode in {"discards", "restarted-discards"}:
        observations = iter([0, 1] if mode == "discards" else [0, 0, 1])

        def discarded(_run: command.Smoke) -> int:
            return next(observations)

        monkeypatch.setattr(checks, "discarded", discarded)
    if mode in {"otlp", "point"}:
        original = checks.post

        def post(run: command.Smoke, signal: str, body: bytes) -> bool:
            return (
                False
                if engine.delta == "2" and ((body == b"{}") == (mode == "otlp"))
                else original(run, signal, body)
            )

        monkeypatch.setattr(checks, "post", post)
    if mode in {"values", "restarted-value"}:
        original_query = http.instant

        def instant(expression: str) -> bytes:
            if expression.startswith("o11y_smoke_delta_") and (
                mode == "values" or engine.delta == "2"
            ):
                return vector([0])
            return original_query(expression)

        monkeypatch.setattr(http, "instant", instant)
    with pytest.raises(command.SmokeError, match="^11: " + rule + "$"):
        checks.group11(run)


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("owner", "owner-rerun"),
        ("identical", "identical-container-states"),
        ("changed", "changed-container-states"),
    ],
)
def test_recreation_semantic_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, run = instance_c()
    if mode == "owner":

        def same(_run: command.Smoke) -> str:
            return "same"

        monkeypatch.setattr(lifecycle, "owner_started", same)
    elif mode == "identical":
        original = run.container_ids
        calls = 0

        def states() -> dict[str, tuple[str, str]]:
            nonlocal calls
            calls += 1
            actual = original()
            return (
                {**actual, "loki": ("changed", "2026-10-25T05:38:26Z")}
                if calls == 2
                else actual
            )

        monkeypatch.setattr(run, "container_ids", states)
    else:

        def invalid(_before: object, _after: object) -> str:
            return "fixture-rule"

        monkeypatch.setattr(smoke, "recreated", invalid)
    with pytest.raises(command.SmokeError, match="^8: " + rule + "$"):
        lifecycle.group8(run, SOURCE)


def test_email_receiver_persists_is_failure() -> None:
    _, _, _, run = instance_c()
    with pytest.raises(command.SmokeError, match=r"^13: email-receiver-deleted$"):
        lifecycle.group13(run, SOURCE)


def test_capacity_populated_condition_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, run = instance_c()

    def missing(*_args: object) -> bool:
        return False

    monkeypatch.setattr(checks, "fires", missing)
    with pytest.raises(command.SmokeError, match=r"^capacity: populated-condition$"):
        cap.smoke(run, SOURCE)


def test_capacity_metadata_requires_all_labels_and_fresh_windows() -> None:
    _, _, _, run = instance_c()
    rows = cap.panel(run, "Freshness and source", 1000)
    assert rows is not None
    assert cap.panel_ok("Freshness and source", rows)
    for key, value in (
        ("agent_plan_window", "other"),
        ("agent_account", "wrong"),
        ("agent_capacity_freshness", "wrong"),
        ("agent_capacity_source", "wrong"),
    ):
        bad = json.loads(packed(rows))
        bad[0]["labels"][key] = value
        assert not cap.panel_ok("Freshness and source", bad)
    assert not cap.panel_ok("Freshness and source", rows[:-1])


def test_capacity_projection_and_burn_boundaries() -> None:
    title = "Projected utilization at reset (3 h or 12 h of data, capped at 100%)"
    projected = {"labels": {"agent_plan_window": "7d"}, "values": [None]}
    assert cap.panel_ok(title, [projected])
    for value in (None, float("nan")):
        assert cap.panel_ok(
            title,
            [projected, {"labels": {"agent_plan_window": "other"}, "values": [value]}],
        )
    for rows in (
        [{"labels": {"agent_plan_window": "7d"}, "values": [1]}],
        [{"labels": {"agent_plan_window": "7d"}, "values": [1.5]}],
        [{"labels": {"agent_plan_window": "5h"}, "values": [1.5]}],
        [{"labels": {"agent_plan_window": "5h"}, "values": [0.5]}],
        [
            projected,
            {"labels": {"agent_plan_window": "other"}, "values": [0]},
        ],
    ):
        assert not cap.panel_ok(title, rows)
    assert cap.panel_ok(
        "Claude Code tokens per second (fleet total)", [{"labels": {}, "values": [2.5]}]
    )


def test_capacity_history_accepts_one_or_more_series() -> None:
    title = "Utilization history by account and window"
    row: Document = {"labels": {}, "values": [1]}
    assert cap.panel_ok(title, [row])
    assert cap.panel_ok(title, [row, row])


def test_cleanup_requires_active_release() -> None:
    _, _, _, run = instance_c()
    with pytest.raises(command.SmokeError, match=r"^capacity cleanup: release$"):
        cap.cleanup(run)


def test_cleanup_parses_paths_containing_equals() -> None:
    fs, _, http, run = instance_c()
    new = Path("/temporary=fixture=capacity")
    fs.rename(run.host, new)
    run.data = run.host = new
    run.env["O11Y_DATA_DIR"] = str(new)
    run.env["O11Y_S3_ENV_FILE"] = str(new / "secrets/s3.env")
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    cap.cleanup(run)
    assert http.dashboard_deleted


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("rules", "preserved-rules"),
        ("provenance", "preserved-rules"),
        ("dashboards", "preserved-dashboard-list"),
        ("folder", "preserved-dashboard-provisioning"),
        ("source", "preserved-dashboard-provisioning"),
        ("meta", "preserved-dashboard-provisioning"),
        ("protection", "deletion-protection"),
    ],
)
def test_cleanup_semantic_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, _, http, run = instance_c()
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    if mode in {"rules", "provenance"}:
        http.overrides["/api/v1/provisioning/alert-rules"] = (200, cleanup_rules(mode))
    elif mode == "dashboards":
        http.overrides["/api/search?type=dash-db"] = (200, b"[]")
    elif mode == "protection":
        original = fs.read_text

        monkeypatch.setattr(fs, "read_text", partial(cleanup_read, original, http))
    else:
        meta = {
            "provisioned": False,
            "provisionedExternalId": "wrong.json"
            if mode == "source"
            else "claude-code.json",
            "folderUid": "wrong" if mode == "folder" else "o11y-agents-folder",
        }
        http.overrides["/api/dashboards/uid/o11y-agents-claude-code"] = (
            200,
            b"{}" if mode == "meta" else packed({"meta": meta}),
        )
    with pytest.raises(command.SmokeError, match="^capacity cleanup: " + rule + "$"):
        cap.cleanup(run)


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("health", "8: backend-health"),
        ("owner", "9: stored-org-role"),
        ("copy", "9: restarted-copy-exit"),
        ("id", "9: service-container-id"),
        ("owner-process", "10: repeated-owner-running: several-containers"),
        ("owner-log", "10: owner-log-searched"),
    ],
)
def test_lifecycle_success_status_with_wrong_evidence(mode: str, rule: str) -> None:
    _, engine, http, run = instance_c()
    if mode == "health":
        engine.overrides["{{.State.Health.Status}}"] = Result(0, "starting")
    elif mode == "owner":
        http.overrides["/api/user/orgs"] = (200, b'[{"orgId":1,"role":"Viewer"}]')
    elif mode == "copy":
        engine.overrides["{{.State.Status}}:{{.State.ExitCode}}"] = Result(
            0, "running:0"
        )
    elif mode == "id":
        engine.overrides["ps -q grafana"] = Result(0, "")
    else:
        lifecycle.group8(run, SOURCE)
        if mode == "owner-process":
            engine.overrides["docker ps -q --filter"] = Result(0, "one two")
        else:
            engine.overrides["logs --no-color"] = Result(0, "unrelated")
    if mode == "health":
        operation: Callable[[], None] = partial(lifecycle.health, run, 8)
    elif mode == "owner":
        operation = partial(lifecycle.owner, run, 9)
    elif mode in {"copy", "id"}:
        operation = partial(lifecycle.group9, run)
    else:
        operation = partial(lifecycle.group10, run)
    with pytest.raises(command.SmokeError, match="^" + rule + "$"):
        operation()


@pytest.mark.parametrize(
    "mode", ["changed-health", "direct-health", "direct-owner", "direct-ready"]
)
def test_later_lifecycle_failures_keep_group(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, http, run = instance_c()
    original = engine.run

    def execute(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        failure = lifecycle_failure(mode, engine, http, argv)
        if failure is not None:
            return failure
        return original(argv, env, timeout, input_data=input_data)

    monkeypatch.setattr(engine, "run", execute)
    rule = (
        "8: backend-health"
        if mode == "changed-health"
        else "9: rustfs did not become ready"
        if mode == "direct-ready"
        else "9: stored-org-role"
        if mode == "direct-owner"
        else "9: backend-health"
    )
    operation = (
        (lambda: lifecycle.group8(run, SOURCE))
        if mode == "changed-health"
        else (lambda: lifecycle.group9(run))
    )
    with pytest.raises(command.SmokeError, match="^" + rule + "$"):
        operation()


def test_group10_scans_binary_owner_processes_and_model() -> None:
    fs, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    for match in ("docker logs abcdef012345", " top ", "config --format json"):
        engine.overrides[match] = Result(0, "org Admin reconciled binary" + chr(0xDCFF))
    lifecycle.group10(run)
    for name in ("owner-repeated.log", "processes.log", "model.log"):
        assert b"\xff" in fs.read_bytes(OUT / name)


def test_email_elapsed_uses_difference() -> None:
    _, _, http, run = instance_c()
    http.email_off = True
    ticks = iter([11, 12, 18])
    run.system = replace(run.system, monotonic=lambda: next(ticks))
    lifecycle.group13(run, SOURCE)
    assert run.transcript[-1].endswith("(7 seconds)\n")


def test_printed_model_ignores_real_environment_file_and_overrides() -> None:
    _, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    run.env.update({"PATH": "/pinned", "SENSITIVE": "private"})
    real = run.env["O11Y_S3_ENV_FILE"]
    lifecycle.group10(run)
    _, environment, _ = next(
        call for call in engine.calls if call[0][-3:] == ["config", "--format", "json"]
    )
    assert environment == {"PATH": "/pinned"}
    assert run.env["O11Y_S3_ENV_FILE"] == real


def test_failed_http_with_valid_bodies_never_evaluates() -> None:
    _, _, http, run = instance_c()
    definition = next(
        r for g in RULES["groups"] for r in g["rules"] if r["uid"] == "o11y-disk-low"
    )
    http.overrides["/alert-rules/"] = (500, packed(definition))
    assert checks.evaluate(run, "o11y-disk-low") is None
    assert not any(url.endswith("/api/v1/eval") for _, url, _, _, _ in http.calls)
    http.overrides = {
        "/api/v1/eval": (
            500,
            packed({"results": {"C": {"status": 200, "frames": [frame({}, [0])]}}}),
        )
    }
    assert checks.evaluate(run, "o11y-disk-low") is None
    http.overrides = {
        "/api/dashboards/uid/o11y-health": (500, packed({"dashboard": HEALTH}))
    }
    assert checks.panel(run, "Up: o11y/tempo") is None
    http.overrides = {
        "/api/dashboards/uid/o11y-agents-capacity": (
            500,
            packed({"dashboard": DASHBOARD}),
        )
    }
    assert cap.panel(run, "Live windows", 1000) is None
    http.overrides = {"/api/ds/query": (500, table_frames(windows()))}
    assert cap.panel(run, "Live windows", 1000) is None


def test_service_down_rustfs_row_needs_a_series_seen_within_seven_days() -> None:
    rule = next(
        rule
        for group in RULES["groups"]
        for rule in group["rules"]
        if rule["uid"] == "o11y-service-down"
    )
    query_a = next(d for d in rule["data"] if d["refId"] == "A")
    expr: str = " ".join(query_a["model"]["expr"].split())
    fallback = (
        '(label_replace(vector(0), "job", "o11y/rustfs", "", "") and on() '
        'count(count_over_time(rustfs_start_total{job="o11y/rustfs"}[7d])))'
    )
    assert fallback in expr
    # Every other job keeps its unconditional row of 0.
    for job in ("alloy", "grafana", "loki", "mimir", "tempo"):
        assert f'label_replace(vector(0), "job", "o11y/{job}", "", "")' in expr
        assert f'"o11y/{job}", "", "") and' not in expr
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "Alerting"


@pytest.mark.parametrize("mode", ["firing", "missing-job"])
def test_service_down_baseline_requires_quiet_complete_inventory(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, run = instance_c()
    original = checks.evaluate

    def evaluate(
        run: command.Smoke, uid: str, threshold: int | None = None
    ) -> list[Document] | None:
        rows = original(run, uid, threshold)
        if uid == "o11y-service-down":
            assert rows is not None
            if mode == "missing-job":
                return rows[:-1]
            rows[0]["values"] = [1]
        return rows

    monkeypatch.setattr(checks, "evaluate", evaluate)
    with pytest.raises(command.SmokeError, match=r"^7: service-down-coverage$"):
        checks.group7(run, SOURCE)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('Starting: rustfs\n{"console_enabled":true}\n', "rustfs-start-log"),
        ('{"console_enabled":false}\n', "rustfs-json-after-start"),
        (
            'Starting: rustfs\n{"console_enabled":false}\nexporter dump\n',
            "rustfs-json-after-start",
        ),
    ],
)
def test_rustfs_stdout_rules(text: str, rule: str) -> None:
    _, engine, _, run = instance_c()
    engine.overrides["logs --no-log-prefix rustfs"] = Result(0, text)
    with pytest.raises(command.SmokeError, match="^7: " + rule + "$"):
        checks.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))


def test_rustfs_allowlist_ignores_comment_prototypes() -> None:
    _, _, _, run = instance_c()
    text = run.fs.read_text(SOURCE / "config/alloy/config.alloy")
    marker = 'otelcol.processor.filter "self" {'
    text = text.replace(marker, marker + "\n// " + marker)
    end = text.index("\n}", text.index(marker))
    text = text[:end] + "\n// " + marker + text[end:]
    checks.rustfs(run, text)


def test_rustfs_allowlist_excludes_later_comments() -> None:
    _, _, http, run = instance_c()
    text = run.fs.read_text(SOURCE / "config/alloy/config.alloy")
    end = text.index("\n}", text.index('otelcol.processor.filter "self" {')) + 2
    text = text[:end] + '\n// "rustfs_forbidden_metric"\n' + text[end:]
    http.overrides["/api/v1/series?"] = (
        200,
        b'{"data":[{"__name__":"rustfs_forbidden_metric"}]}',
    )
    with pytest.raises(command.SmokeError, match=r"^7: rustfs-metric-allowlist$"):
        checks.rustfs(run, text)


@pytest.mark.parametrize("trailer", ["200X", "200 ", "500", "200\n500"])
def test_discard_scrape_keeps_exact_status(trailer: str) -> None:
    _, engine, _, run = instance_c()
    engine.overrides["http://mimir:8080/metrics"] = Result(
        0, "# TYPE cortex_build_info gauge\n\n" + trailer
    )
    with pytest.raises(command.SmokeError, match=r"^11: discard-scrape$"):
        checks.discarded(run)


def test_fires_passes_an_explicit_threshold() -> None:
    _, _, _, run = instance_c()
    assert checks.fires(run, "o11y-rustfs-memory", "job", ["o11y/rustfs"], 0)


@pytest.mark.parametrize(
    ("mode", "rule"),
    [("stopped-rule", "stopped-tempo-rule"), ("recovery", "recovered-scrape-jobs")],
)
def test_monitoring_later_rule_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, http, run = instance_c()
    original = checks.evaluate

    def evaluate(
        run: command.Smoke, uid: str, threshold: int | None = None
    ) -> list[Document] | None:
        if uid == "o11y-service-down" and "tempo" in engine.stopped:
            return [{"labels": {}, "values": [0]}]
        return original(run, uid, threshold)

    if mode == "stopped-rule":
        monkeypatch.setattr(checks, "evaluate", evaluate)
    else:
        original_query = http.instant
        attempts = 0

        def instant(expression: str) -> bytes:
            nonlocal attempts
            if expression == "up == 1":
                attempts += 1
                if attempts > 1:
                    return vector([])
            return original_query(expression)

        monkeypatch.setattr(http, "instant", instant)
    with pytest.raises(command.SmokeError, match="^7: " + rule + "$"):
        checks.group7(run, SOURCE)


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("push", "rustfs-push-identity"),
        ("resource", "rustfs-resource-identity"),
        ("rustfs_node", "rustfs-resource-identity"),
        ("network_local_address", "rustfs-resource-identity"),
        ("traces", "rustfs-no-traces"),
        ("logs", "rustfs-no-otlp-logs"),
    ],
)
def test_rustfs_bad_identity_and_forbidden_signals(mode: str, rule: str) -> None:
    _, _, http, run = instance_c()
    if mode in {"push", "resource"}:
        http.overrides[
            "query=" + ("rustfs_start_total" if mode == "push" else "target_info")
        ] = (
            200,
            packed({"status": "success", "data": {"result": [{"value": [0, "1"]}]}}),
        )
    elif mode in {"rustfs_node", "network_local_address"}:
        http.overrides["query=target_info"] = (
            200,
            vector(
                [1],
                [
                    {
                        "host_name": "smoke-host",
                        "deployment_environment_name": "smoke",
                        mode: "obsolete",
                    }
                ],
            ),
        )
    elif mode == "traces":
        http.overrides["/uid/tempo/api/search?"] = (200, b'{"traces":[{}]}')
    else:
        http.overrides["/uid/loki/loki/api/v1/query_range?"] = (200, vector([1]))
    config = run.fs.read_text(SOURCE / "config/alloy/config.alloy")

    def act() -> None:
        # The push is awaited in group7, before the service-down baseline.
        if mode == "push":
            checks.group7(run, SOURCE)
        else:
            checks.rustfs(run, config)

    with pytest.raises(command.SmokeError, match="^7: " + rule + "$"):
        act()


def test_rustfs_entire_allowlist_and_histogram_suffixes() -> None:
    _, _, http, run = instance_c()
    config = run.fs.read_text(SOURCE / "config/alloy/config.alloy")
    match = re.search(r'otelcol.processor.filter "self" \{(.*?)\n\}', config, re.DOTALL)
    assert match is not None
    block = match[1]
    names = re.findall(r'"(rustfs_[a-z0-9_]+)"', block)
    histogram = "rustfs_http_server_request_duration_seconds"
    names.remove(histogram)
    names.extend(histogram + suffix for suffix in ("_bucket", "_sum", "_count"))
    http.overrides["/api/v1/series?"] = (
        200,
        packed({"data": [{"__name__": name} for name in names]}),
    )
    checks.rustfs(run, config)


def test_rustfs_start_line_can_follow_prelude_and_use_crlf() -> None:
    _, engine, _, run = instance_c()
    engine.overrides["logs --no-log-prefix rustfs"] = Result(
        0, 'entrypoint prelude\r\nStarting: rustfs\r\n{"console_enabled":false}\r\n'
    )
    checks.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))
    engine.overrides["logs --no-log-prefix rustfs"] = Result(
        0,
        "entrypoint prelude\nStarting: rustfs\nexporter "
        'dump\n{"console_enabled":false}\n',
    )
    with pytest.raises(command.SmokeError, match=r"^7: rustfs-json-after-start$"):
        checks.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))


def test_discard_exposition_and_trailing_newline() -> None:
    _, engine, _, run = instance_c()
    engine.overrides["http://mimir:8080/metrics"] = Result(
        0, "# TYPE cortex_build_info gauge\n\n200\n"
    )
    assert checks.discarded(run) == 0
    engine.overrides["http://mimir:8080/metrics"] = Result(0, "not an exposition\n200")
    with pytest.raises(command.SmokeError, match=r"^11: discard-exposition$"):
        checks.discarded(run)


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("series", "baseline-series-count"),
        ("rate-upload", "rate-log-upload"),
        ("webhook", "webhook-delivery"),
        ("smtp", "smtp-delivery"),
    ],
)
def test_alert_late_semantic_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, http, run = instance_c()
    if mode == "series":
        original = checks.evaluate

        def evaluate(
            run: command.Smoke, uid: str, threshold: int | None = None
        ) -> list[Document] | None:
            return (
                []
                if uid == "o11y-log-rate" and not http.rate
                else original(run, uid, threshold)
            )

        monkeypatch.setattr(checks, "evaluate", evaluate)
    elif mode == "rate-upload":
        original_post = checks.post

        def post(run: command.Smoke, signal: str, body: bytes) -> bool:
            return (
                False
                if b"o11y-smoke-rate" in body
                else original_post(run, signal, body)
            )

        monkeypatch.setattr(checks, "post", post)
    elif mode == "webhook":
        engine.overrides["cat " + checks.WEBHOOK_POSTS] = Result(0, "unrelated")
    else:
        engine.overrides["http://127.0.0.1:8025/api/v1/messages"] = Result(0, "{}")
    with pytest.raises(command.SmokeError, match="^12: " + rule + "$"):
        checks.group12(run, SOURCE)


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("matching", [True, False])
def test_smtp_failure_evidence(count: int, matching: bool) -> None:
    fs, engine, _, run = instance_c()
    log = "SMTP authentication failed\nemail delivery failed\nnotify retry\n"
    engine.overrides["logs --no-color grafana"] = Result(
        0,
        "unrelated line\n" + (log if matching else ""),
        "nOtIfY stderr\n" if matching else "other stderr\n",
    )
    engine.overrides[checks.MAIL_MESSAGES] = Result(0, json.dumps({"total": count}))
    checks.smtp_diagnostics(run)
    assert fs.read_text(run.artifacts / "grafana-smtp.log") == (
        log + "nOtIfY stderr\n" if matching else "no SMTP, email or notify log lines\n"
    )
    assert fs.read_text(run.artifacts / "mailpit-count.log") == f"messages={count}\n"
    assert [
        (path, mode)
        for kind, path, mode in (c for c in fs.calls if c[0] == "write")
        if run.artifacts in path.parents
    ] == [
        (run.artifacts / "grafana-smtp.log", 0o600),
        (run.artifacts / "mailpit-count.log", 0o600),
    ]
    calls = engine.calls[-2:]
    assert calls[0][0][-3:] == ["logs", "--no-color", "grafana"]
    assert calls[1][0][-6:] == [
        "exec",
        "-T",
        "alert-mail",
        "wget",
        "-qO-",
        checks.MAIL_MESSAGES,
    ]
    assert "--profile" in calls[1][0]
    assert [timeout for _, _, timeout in calls] == [120, 120]


@pytest.mark.parametrize("target", ["logs --no-color grafana", checks.MAIL_MESSAGES])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_smtp_diagnostics_search_raw_captures(target: str, stream: str) -> None:
    fs, engine, _, run = instance_c()
    for needle in run.sentinels.scan_values():
        raw = "unrelated " + needle.decode()
        engine.overrides["logs --no-color grafana"] = Result(0, "smtp error\n")
        engine.overrides[checks.MAIL_MESSAGES] = Result(0, '{"total":0}')
        engine.overrides[target] = (
            Result(0, raw) if stream == "stdout" else Result(0, "safe", raw)
        )
        with pytest.raises(
            command.SmokeError, match=r"^diagnostics: SMTP capture unsafe$"
        ):
            checks.smtp_diagnostics(run)
        assert not any(run.artifacts in path.parents for path in fs.files)


@pytest.mark.parametrize("target", ["logs --no-color grafana", checks.MAIL_MESSAGES])
@pytest.mark.parametrize("result", [Result(1, "failed"), Result(0)])
def test_smtp_diagnostics_refuse_failed_or_empty_capture(
    target: str, result: Result
) -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[checks.MAIL_MESSAGES] = Result(0, '{"total":0}')
    engine.overrides[target] = result
    with pytest.raises(command.SmokeError, match=r"^diagnostics: SMTP capture "):
        checks.smtp_diagnostics(run)
    assert not any(run.artifacts in path.parents for path in fs.files)


@pytest.mark.parametrize(
    "body", ["{}", "[]", '{"total":true}', '{"total":-1}', '{"total":"0"}', chr(0xDCFF)]
)
def test_smtp_diagnostics_refuse_invalid_count(body: str) -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[checks.MAIL_MESSAGES] = Result(0, body)
    with pytest.raises(
        command.SmokeError, match=r"^diagnostics: mail count unavailable$"
    ):
        checks.smtp_diagnostics(run)
    assert not any(run.artifacts in path.parents for path in fs.files)


def test_smtp_failure_keeps_delivery_error_when_capture_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[checks.MAIL_MESSAGES] = Result(0, '{"total":0}')

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(fs, "write_atomic", denied)
    with pytest.raises(command.SmokeError, match=r"^12: smtp-delivery$"):
        checks.group12(run, SOURCE)
    assert run.transcript[-1] == "smoke diagnostics: SMTP capture or scan unavailable\n"


def test_smtp_failure_evidence_survives_finish_with_backend_logs() -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[checks.MAIL_MESSAGES] = Result(0, '{"total":0}')
    engine.overrides["logs --no-color grafana"] = Result(
        0, "smtp authentication failed\n"
    )
    with pytest.raises(command.SmokeError, match=r"^12: smtp-delivery$"):
        checks.group12(run, SOURCE)
    # finish must append its normal backend diagnostics to the stage's evidence.
    run.emit("FAIL 12: smtp-delivery")
    output = io.StringIO()
    run.finish(1, output)
    assert "clean Alloy and Loki logs retained" in output.getvalue()
    assert "capture or scan failed" not in output.getvalue()
    assert fs.list_dir(run.artifacts) == (
        "alloy.log",
        "grafana-smtp.log",
        "loki-head.log",
        "loki.log",
        "mailpit-count.log",
    )
    assert (
        fs.read_text(run.artifacts / "grafana-smtp.log")
        == "smtp authentication failed\n"
    )
    assert fs.read_text(run.artifacts / "mailpit-count.log") == "messages=0\n"
    assert fs.modes[run.artifacts] & 0o777 == 0o700
    assert fs.lstat(run.out) is None
    assert fs.lstat(run.host) is None


@pytest.mark.parametrize("draw", [b"\x01", b"\x02"])
def test_mailpit_reads_the_run_smtp_secret(tmp_path: Path, draw: bytes) -> None:
    config = yaml.safe_load((ROOT / "o11y/compose.tools.yaml").read_text())
    service = config["services"]["alert-mail"]
    assert service["volumes"] == [
        "${O11Y_DATA_DIR:?}/secrets/alert_smtp_password:/run/o11y/alert_smtp_password:ro"
    ]
    assert service["entrypoint"] == ["/bin/sh", "-c"]
    assert service["profiles"] == ["tools"]
    password = dict(
        smoke.sentinels(
            (*(draw * s.length for s in init.SECRETS), draw * 16, draw * 16)
        ).values
    )[init.SMTP]
    secret = tmp_path / "secret"
    auth = tmp_path / "auth"
    secret.write_bytes(password)
    script = (
        service["command"][0]
        .replace("/run/o11y/alert_smtp_password", str(secret))
        .replace(str(Path("/") / "tmp" / "smtp-auth"), str(auth))
        .replace("exec /mailpit", "exec true")
    )
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, check=False)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert (
        hashlib.sha256(auth.read_bytes()).digest()
        == hashlib.sha256(b"smoke-user:" + password + b"\n").digest()
    )
    assert auth.stat().st_mode & 0o777 == 0o600
    assert "--smtp-auth-file" in script
    assert "--smtp-require-starttls" in script
    assert "--smtp-tls-cert sans:alert-mail --smtp-tls-key sans:alert-mail" in script
    secret.unlink()
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, check=False)
    assert result.returncode != 0


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
    run: command.Smoke,
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


def cleanup_read(original: Callable[[Path], str], http: HttpC, path: Path) -> str:
    text = original(path)
    if http.deleted_rules and path.name == "agents.yaml":
        document = yaml.safe_load(text)
        document["providers"][0]["disableDeletion"] = False
        return yaml.safe_dump(document)
    return text


def lifecycle_failure(
    mode: str, engine: EngineC, http: HttpC, argv: Sequence[str]
) -> Result | None:
    direct = any(call[0][1] == "start" for call in engine.calls)
    if (
        mode == "changed-health"
        and engine.owner_time >= 2
        and "{{.State.Health.Status}}" in argv
    ):
        return Result(0, "unhealthy")
    if direct and mode == "direct-health" and "{{.State.Health.Status}}" in argv:
        return Result(0, "unhealthy")
    if direct and mode == "direct-ready" and argv[-1] == smoke.READY["rustfs"]:
        return Result(0, "503")
    if direct and mode == "direct-owner":
        http.overrides["/api/user/orgs"] = (200, b'[{"orgId":1,"role":"Viewer"}]')
    return None


def cleanup_rules(mode: str) -> bytes:
    rules = [
        {"uid": r["uid"], "provenance": "file"}
        for g in RULES["groups"]
        if g["name"] != "agent-capacity"
        for r in g["rules"]
    ]
    if mode == "rules":
        rules.pop()
    else:
        rules[0]["provenance"] = "api"
    return packed(rules)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (Result(0, ""), "zero-containers"),
        (Result(0, "abcdef012345 fedcba543210"), "several-containers"),
        (Result(0, "fedcba543210"), "identity-mismatch"),
        (Result(1, "private"), "container-query"),
    ],
)
def test_repeated_owner_identity_failure(answer: Result, reason: str) -> None:
    _, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    engine.overrides["docker ps -q --filter"] = answer
    with pytest.raises(
        command.SmokeError, match="^10: repeated-owner-running: " + reason + "$"
    ):
        lifecycle.group10(run)


@pytest.mark.parametrize("printed", ["abcdef012345", "abcdef012345" + "6" * 52])
def test_repeated_owner_short_and_full_ids(printed: str) -> None:
    _, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    engine.overrides["run --rm -d"] = Result(0, printed + "\n")
    lifecycle.group10(run)
    for operation in ("top", "logs", "stop"):
        assert any(
            argv == ["docker", operation, "abcdef012345"] for argv, _, _ in engine.calls
        )


@pytest.mark.parametrize(
    "line", ["private", "=private", "  =private", "bad key=private", "1KEY=private"]
)
def test_cleanup_malformed_env_redacts_value(line: str) -> None:
    fs, _, http, run = instance_c()
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    assert run.release is not None
    fs.write_atomic(run.release / ".env", line, 0o600)
    with pytest.raises(
        command.SmokeError, match=r"^capacity cleanup: malformed-env-line$"
    ):
        cap.cleanup(run)


def test_cleanup_env_comments_and_blank_lines() -> None:
    fs, _, http, run = instance_c()
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    assert run.release is not None
    path = run.release / ".env"
    fs.write_atomic(
        path, "\n  \n# comment\n  # indented comment\n" + fs.read_text(path), 0o600
    )
    cap.cleanup(run)
    assert http.dashboard_deleted


@pytest.mark.parametrize("disable_fails", [False, True])
def test_cleanup_restore_failure_keeps_original(
    disable_fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, http, run = instance_c()
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    original = cap.reload
    count = 0

    def reload(run: command.Smoke, kind: str) -> None:
        nonlocal count
        count += 1
        if count == 1 and disable_fails:
            message = "capacity cleanup: disable-failed"
            raise command.SmokeError(message)
        if count == (2 if disable_fails else 3):
            message = "capacity cleanup: restore-failed"
            raise command.SmokeError(message)
        original(run, kind)

    monkeypatch.setattr(cap, "reload", reload)
    with pytest.raises(
        command.SmokeError,
        match="^capacity cleanup: "
        + ("disable-failed" if disable_fails else "restore-failed")
        + "$",
    ):
        cap.cleanup(run)
    assert (
        "FAIL capacity cleanup: restore-deletion-protection\n" in run.transcript
    ) == disable_fails


def test_expired_inventory_requires_live_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, run = instance_c()
    original = cap.panel
    expired = False

    def panel(run: command.Smoke, title: str, now: int) -> list[Document] | None:
        nonlocal expired
        if now > 2200:
            expired = True
        if expired and title == "Live windows":
            return []
        return original(run, title, now)

    monkeypatch.setattr(cap, "panel", panel)
    with pytest.raises(command.SmokeError, match=r"^capacity: expired-inventory$"):
        cap.smoke(run, SOURCE)


def test_repeated_owner_waits_for_labelled_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    original = engine.run
    observations = iter((Result(0, ""), Result(0, "abcdef012345\n")))

    def execute(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        if argv[1:3] == ["ps", "-q"]:
            return next(observations)
        return original(argv, env, timeout, input_data=input_data)

    monkeypatch.setattr(engine, "run", execute)
    lifecycle.group10(run)
    assert list(observations) == []


@pytest.mark.parametrize(
    "answer",
    [
        Result(0, "grafana-owner: org Admin reconciled\n", "cycle complete\n"),
        Result(0, "cycle complete\n", "grafana-owner: org Admin reconciled\n"),
    ],
)
def test_repeated_owner_waits_for_full_cycle(
    answer: Result, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    original = engine.run
    observations = iter((Result(0), answer))
    started = run.system.monotonic()

    def execute(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        if argv == ["docker", "logs", "abcdef012345"]:
            return next(observations)
        return original(argv, env, timeout, input_data=input_data)

    monkeypatch.setattr(engine, "run", execute)
    lifecycle.group10(run)
    assert list(observations) == []
    assert run.system.monotonic() - started == 2
    assert (
        fs.read_bytes(OUT / "owner-repeated.log")
        == (answer.stdout + answer.stderr).encode()
    )
    assert (["docker", "stop", "abcdef012345"], run.env, 120) in engine.calls


@pytest.mark.parametrize(
    "answer",
    [Result(0, ""), Result(0, "cycle starting\n"), Result(1, "org Admin reconciled")],
)
def test_repeated_owner_output_timeout(answer: Result) -> None:
    fs, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    engine.overrides["docker logs abcdef012345"] = answer
    started = run.system.monotonic()
    with pytest.raises(command.SmokeError, match=r"^10: repeated-owner-output$"):
        lifecycle.group10(run)
    assert run.system.monotonic() - started == 30
    assert fs.lstat(OUT / "owner-repeated.log") is None
    assert (["docker", "stop", "abcdef012345"], run.env, 120) in engine.calls


@pytest.mark.parametrize(
    "printed",
    [
        "abcdef012345" + "6" * 51,
        "abcdef012345" + "6" * 53,
        "abcdef012345\nprivate-extra-line",
        "private-container-name",
    ],
)
def test_repeated_owner_rejects_other_printed_identities(printed: str) -> None:
    _, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    engine.overrides["run --rm -d"] = Result(0, printed)
    with pytest.raises(
        command.SmokeError, match=r"^10: repeated-owner-running: identity-mismatch$"
    ):
        lifecycle.group10(run)


def test_repeated_owner_running_failure_survives_stop_failure() -> None:
    _, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    engine.overrides["docker ps -q --filter"] = Result(0, "")
    engine.overrides["docker stop"] = Result(1, "private")
    with pytest.raises(
        command.SmokeError, match=r"^10: repeated-owner-running: zero-containers$"
    ):
        lifecycle.group10(run)


@pytest.mark.parametrize(
    "baseline",
    [
        ("disk-low", ["o11y/rustfs"], None),
        ("rustfs-memory", [], None),
        ("rustfs-memory", ["o11y/rustfs"], 0),
        ("ingest-dropped", [], None),
        ("alloy-export", [], None),
        ("log-rate", [], None),
        ("log-rate", [], 0),
    ],
)
@pytest.mark.parametrize(
    ("rows", "observed"),
    [
        (None, "no usable result"),
        (
            [
                {"labels": {"job": "z"}, "values": [1]},
                {"labels": {"job": "a"}, "values": [1]},
                {"labels": {"job": "ignored"}, "values": [0]},
            ],
            "['a', 'z']",
        ),
        ([{"values": []}], "malformed row"),
    ],
)
def test_baseline_failure_diagnostic(
    baseline: tuple[str, list[str], int | None],
    rows: list[Document] | None,
    observed: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, expected, threshold = baseline
    _, _, _, run = instance_c()
    run.transcript.clear()
    original = checks.evaluate

    def evaluate(
        run: command.Smoke, rule: str, limit: int | None = None
    ) -> list[Document] | None:
        if (rule, limit) == ("o11y-" + uid, threshold):
            return rows
        return original(run, rule, limit)

    monkeypatch.setattr(checks, "evaluate", evaluate)
    with pytest.raises(command.SmokeError, match=r"^12: baseline-conditions$"):
        checks.group12(run, SOURCE)
    assert run.transcript == [
        (
            f"smoke diagnostics: rule=o11y-{uid} threshold={threshold} "
            f"expected={expected} observed={observed}\n"
        )
    ]
