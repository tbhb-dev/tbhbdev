"""In-memory Grafana access and export seams for smoke tests."""

import io
import json
from contextlib import redirect_stderr
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
lazy from collections.abc import Mapping, Sequence

from tbhbops.o11y import init, smoke
from tbhbops.o11y import smoke_access as access
from tbhbops_cli import cli
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    bootstrap,
    runtime,
    transport,
)
from tbhbops_cli.o11y.smoke import setup as setup_stage
from test_o11y_cli import Fake
from test_receipt import Http as ReceiptHttp
from unit.o11y.smoke.runtime_fixture import ARTIFACTS, HOST, OUT, Engine, make_system
lazy from tbhbops.o11y.validate import Document


def packed(data: object) -> bytes:
    return json.dumps(data).encode()


def dashboard_answers() -> Document:
    return {
        "/api/search?type=dash-db": [
            {"uid": uid, "folderUid": access.FOLDER if uid in access.AGENTS else ""}
            for uid in (*access.AGENTS, "o11y-github-activity", access.HEALTH)
        ],
        f"/api/folders/{access.FOLDER}": {"uid": access.FOLDER, "title": "Agents"},
        **{
            f"/api/dashboards/uid/{uid}": {
                "dashboard": {"uid": uid},
                "meta": {
                    "provisioned": False,
                    "provisionedExternalId": access.DASHBOARD_FILES[uid],
                    "folderUid": access.FOLDER if uid in access.AGENTS else "",
                },
            }
            for uid in (*access.AGENTS, "o11y-github-activity", access.HEALTH)
        },
    }


@dataclass
class Http:
    fixtures: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, str, bytes | None, dict[str, str], int]] = field(
        default_factory=list
    )
    webhook: str = "synthetic"
    overrides: dict[str, tuple[int, bytes] | Exception] = field(default_factory=dict)
    pending: int = 0
    receipt: ReceiptHttp = field(default_factory=ReceiptHttp)

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: int,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, body, dict(headers), timeout))
        historical = parse_qs(urlsplit(url).query).get("query") == ["production"]
        assert timeout == (30 if historical else 15)
        assert url.startswith((transport.GRAFANA, transport.OTLP))
        assert not any(
            key.lower() in {"authorization", "cookie", "proxy-authorization"}
            for key in headers
        )
        for match, answer in self.overrides.items():
            if match not in url:
                continue
            if isinstance(answer, Exception):
                raise answer
            return answer
        if method == "POST" and url.startswith(transport.OTLP):
            return self.post(url, body)
        if "/api/datasources/proxy/" in url:
            return self.query(url)
        return self.grafana(
            method,
            url.removeprefix(transport.GRAFANA),
            headers.get("Tailscale-User-Login", ""),
        )

    def post(self, url: str, body: bytes | None) -> tuple[int, bytes]:
        if not url.endswith("/v1/traces"):
            return 200, b"x" * 37
        assert body is not None
        data = json.loads(body)
        service = data["resourceSpans"][0]["resource"]["attributes"][0]["value"][
            "stringValue"
        ]
        scope = data["resourceSpans"][0]["scopeSpans"][0]
        if scope["scope"]["name"] == "o11y-filter-check":
            trace = scope["spans"][0]["traceId"]
            if service == "codex_exec":
                scope["spans"] = [scope["spans"][i] for i in (0, 7, 8, 9)]
            elif service == "codex_tui":
                scope["spans"] = scope["spans"][:1]
            self.fixtures[trace] = packed({"batches": data["resourceSpans"]})
        return 200, b"{}"

    def query(self, url: str) -> tuple[int, bytes]:
        trace = url.rsplit("/", 1)[1]
        if "/api/traces/" in url and trace in self.fixtures:
            if self.pending:
                self.pending -= 1
                return 503, b"private-body"
            return 200, self.fixtures[trace]
        query = parse_qs(urlsplit(url).query)
        if "time" in query:
            return 200, b'{"data":{"result":[{"value":[0,"49"]}]}}'
        if "step" in query and query["query"] == ["production"]:
            begin, end = int(query["start"][0]), int(query["end"][0])
            return 200, packed(
                {
                    "status": "success",
                    "data": {
                        "result": [
                            {
                                "metric": {"component_id": f"smoke-{i}-ongoing"},
                                "values": [[t, "1"] for t in range(begin, end + 1, 30)],
                            }
                            for i in range(7)
                        ]
                    },
                }
            )
        return self.receipt.request("GET", url, None, {}, 15)

    def grafana(self, method: str, path: str, login: str) -> tuple[int, bytes]:
        answers = {
            **dashboard_answers(),
            "/api/user": {"login": login, "isGrafanaAdmin": login == "admin"},
            "/api/user/orgs": [
                {"orgId": 1, "role": "Editor" if login == access.VIEWER else "Admin"}
            ],
            "/api/datasources": [{"uid": uid} for uid in ("tempo", "mimir", "loki")],
            "/api/v1/provisioning/contact-points": [
                {
                    "uid": f"o11y-{kind}",
                    "name": "o11y",
                    "provenance": "file",
                    "type": kind,
                    "settings": settings,
                }
                for kind, settings in (
                    ("discord", {"url": "[REDACTED]", "use_discord_username": False}),
                    (
                        "email",
                        {"addresses": "smoke-to@example.invalid", "singleEmail": True},
                    ),
                )
            ],
            "/api/v1/provisioning/contact-points/export?decrypt=true&format=json": {
                "contactPoints": [
                    {
                        "receivers": [
                            {"uid": "o11y-discord", "settings": {"url": self.webhook}}
                        ]
                    }
                ]
            },
            "/api/v1/provisioning/alert-rules": [{"uid": uid} for uid in access.RULES],
            "/api/v1/provisioning/alert-rules/o11y-alloy-export": {
                "for": "2m",
                "data": [{"refId": "A", "model": {"expr": "production"}}],
            },
            "/api/org/preferences": {"homeDashboardUID": "o11y-health"},
            "/api/dashboards/home": {"redirectUri": "/d/o11y-health/health"},
            "/api/org": {},
        }
        if path.endswith("export?decrypt=true"):
            return 403, b"private-body"
        if method != "GET" and path != "/api/org/preferences":
            return (200 if login == access.OWNER else 403), b"private-body"
        return 200, packed(answers[path])


@dataclass
class Admin:
    calls: list[tuple[str, bytes, int]] = field(default_factory=list)
    answer: tuple[int, bytes] | Exception | None = None

    def request(
        self, url: str, password: bytes, timeout: int, *, method: str
    ) -> tuple[int, bytes]:
        assert method == "GET"
        self.calls.append((url, password, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is not None:
            return self.answer
        return (403 if password == b"admin" else 200), b"private-body"


@dataclass
class EngineB(Engine):
    fs: Fake
    calls: list[tuple[list[str], dict[str, str], int]] = field(default_factory=list)
    overrides: dict[str, Result] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    upload: str = "{}\n200"
    inputs: list[str | None] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        self.inputs.append(input_data)
        for match, answer in self.overrides.items():
            if match in " ".join(argv):
                self.calls.append((list(argv), dict(env), timeout))
                return answer
        if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "find":
            self.calls.append((list(argv), dict(env), timeout))
            assert argv[-4:] == ["find", "rustfs:pinned", "-mindepth", "1"] or argv[
                -5:
            ] == ["rustfs:pinned", "/d/loki", "-mindepth", "1", "-delete"]
            paths: list[Path] = list(self.fs.modes)
            for path in paths:
                if HOST / "loki" in path.parents:
                    self.fs.modes.pop(path)
                    self.fs.files.pop(path, None)
            return Result(0)
        if argv[1:3] == ["run", "--rm"] and "traces" in argv:
            self.calls.append((list(argv), dict(env), timeout))
            return Result(0, "private-body", "private-error")
        result: Result = super().run(argv, env, timeout)
        return result

    def compose(self, argv: Sequence[str], env: Mapping[str, str]) -> Result:
        if "config" in argv and argv[-2:] == ["--format", "json"]:
            rendered: Result = super().compose(argv, env)
            data = json.loads(rendered.stdout)
            data["services"].update(
                {
                    "telemetrygen": {"image": "telemetrygen:pinned"},
                    "grafana-owner": {
                        "user": "472:472",
                        "depends_on": {"grafana": {"condition": "service_healthy"}},
                        "volumes": [
                            {
                                "target": "/run/o11y/grafana_admin_password",
                                "read_only": True,
                            }
                        ],
                    },
                }
            )
            return Result(0, json.dumps(data))
        if "exec" in argv:
            answer = self.inside_response(argv)
            if answer is not None:
                return answer
        if "s3probe" in argv and "count" in argv:
            bucket = argv[-1]
            remaining = self.counts.get(bucket, 0)
            self.counts[bucket] = max(remaining - 1, 0)
            return Result(0, "0" if remaining else "1")
        if "logs" in argv and argv[-1] == "grafana":
            return Result(0, "provisioning level=info")
        result: Result = super().compose(argv, env)
        return result

    def inside_response(self, argv: Sequence[str]) -> Result | None:
        if "GF_AUTH_PROXY_WHITELIST" in argv[-1]:
            return Result(0, "127.0.0.1\n")
        if argv[-1] == "http://mimir:8080/otlp/v1/metrics":
            return Result(0, self.upload)
        if argv[-1] in ("http://loki:3100/flush", "http://mimir:8080/ingester/flush"):
            return Result(0, "204")
        if argv[-1].startswith("http://grafana:"):
            return Result(
                0,
                "401"
                if argv[-1].endswith("/user")
                else "403"
                if "POST" in argv
                else "200",
            )
        if argv[-1].endswith("/loki/api/v1/push"):
            return Result(0, "\n204")
        return None


def setup_b() -> tuple[Fake, EngineB, Http, cli.System]:
    fs, _, system = make_system()
    engine = EngineB(fs)
    http = Http()
    draws = 0

    def random(size: int) -> bytes:
        nonlocal draws
        draws += 1
        assert draws < 40
        # Keep secrets distinct from the receipt fake's 01 IDs in the transcript.
        # Span traces get unique IDs too.
        return bytes([2 if draws <= 7 else 1 if draws <= 11 else draws - 1]) * size

    ticks: list[float] = [0]

    def sleep(seconds: float) -> None:
        assert seconds == 2
        ticks[0] += seconds
        assert ticks[0] <= 1000

    system = replace(
        system,
        runner=engine,
        receipt_http=http,
        admin_http=Admin(),
        random_bytes=random,
        epoch=lambda: 1000.0,
        monotonic=lambda: ticks[0],
        sleep=sleep,
    )
    return fs, engine, http, system


def instance_b() -> tuple[Fake, EngineB, Http, runtime.Smoke]:
    fs, engine, http, system = setup_b()
    for _ in range(7):
        system.random_bytes(1)
    # Run the actual preceding stages to obtain the init-written secret file.
    sentinels = smoke.sentinels(
        (*(b"\1" * s.length for s in init.SECRETS), b"\1" * 16, b"\1" * 16)
    )
    run = runtime.Smoke(
        system,
        OUT,
        ARTIFACTS,
        HOST,
        HOST,
        "o11y-smoke-fixture",
        sentinels,
        runtime.environment({}, HOST, (123, 456)),
    )
    setup_stage.prepare(run, Path("/stack"))
    setup_stage.group1(run)
    bootstrap.group2(run)
    http.webhook = dict(sentinels.values)[init.WEBHOOK].decode()
    return fs, engine, http, run


def execute(system: cli.System, *tail: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stderr(err):
        try:
            code = cli.run(
                [
                    "o11y",
                    "smoke",
                    "--source",
                    "/stack",
                    "--out",
                    str(OUT),
                    "--artifacts",
                    str(ARTIFACTS),
                    *tail,
                ],
                system,
                out,
                err,
            )
        except SystemExit as exc:
            code = int(str(exc.code))
    return code, out.getvalue(), err.getvalue()
