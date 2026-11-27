"""Slice B runs with fake HTTP, filesystem, process, entropy and time seams."""

import io
import json
import stat
from contextlib import redirect_stderr
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
lazy from collections.abc import Callable, Mapping, Sequence

import pytest
from tbhbops.o11y import init, smoke
from tbhbops.o11y import smoke_access as access
from tbhbops_cli import cli
from tbhbops_cli.o11y import smoke as command
from tbhbops_cli.o11y import smoke_capacity, smoke_checks, smoke_lifecycle
from tbhbops_cli.o11y import smoke_stages as stages
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.runner import Result
lazy from tbhbops.o11y.validate import Document

from .test_o11y_smoke import ARTIFACTS, HOST, OUT, Engine, setup  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace
from .test_receipt import Http as ReceiptHttp  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace
lazy from .test_o11y_cli import Fake  # pyrefly: ignore[missing-import]  # pytest importlib supplies this namespace


def packed(data: object) -> bytes:
    return json.dumps(data).encode()


@pytest.fixture(autouse=True)
def slice_b_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    def skip_later(*_args: object) -> None:
        pass

    for module, names in (
        (smoke_capacity, ("smoke", "cleanup")),
        (smoke_checks, ("group7", "group11", "group12")),
        (smoke_lifecycle, ("group8", "group9", "group10", "group13")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, skip_later)


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
        assert url.startswith((stages.GRAFANA, stages.OTLP))
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
        if method == "POST" and url.startswith(stages.OTLP):
            return self.post(url, body)
        if "/api/datasources/proxy/" in url:
            return self.query(url)
        return self.grafana(
            method,
            url.removeprefix(stages.GRAFANA),
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
    fs, _, system = setup()
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


def instance_b() -> tuple[Fake, EngineB, Http, command.Smoke]:
    fs, engine, http, system = setup_b()
    for _ in range(7):
        system.random_bytes(1)
    # Run the actual preceding stages to obtain the init-written secret file.
    sentinels = smoke.sentinels(
        (*(b"\1" * s.length for s in init.SECRETS), b"\1" * 16, b"\1" * 16)
    )
    run = command.Smoke(
        system,
        OUT,
        ARTIFACTS,
        HOST,
        HOST,
        "o11y-smoke-fixture",
        sentinels,
        command.environment({}, HOST, (123, 456)),
    )
    run.prepare(Path("/stack"))
    run.group1()
    run.group2()
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


def test_group4_password_and_proxy_contract() -> None:
    fs, engine, http, run = instance_b()
    password: bytes = fs.read_bytes(HOST / "secrets/grafana_admin_password")
    stages.group4(run)
    assert isinstance(run.system.admin_http, Admin)
    assert len(run.system.admin_http.calls) == 2
    assert run.system.admin_http.calls == [
        (stages.GRAFANA + "/api/admin/settings", password, 15),
        (stages.GRAFANA + "/api/admin/settings", b"admin", 15),
    ]
    assert password.decode().strip() not in str(engine.calls)
    assert password.decode().strip() not in str(run.transcript)
    run.finalize_transcript()
    assert password.strip() not in fs.read_bytes(OUT / "transcript.log")
    for login in (access.OWNER, access.VIEWER, "admin"):
        assert any(
            call[1].endswith("/api/user") and call[3] == {"Tailscale-User-Login": login}
            for call in http.calls
        )
    assert (
        run.transcript[-1] == "ok 4: Grafana permissions, proxy trust, recovery login, "
        "provisioning and redaction hold\n"
    )


@pytest.mark.parametrize(
    "answer",
    [(500, b"private-body"), TransportError("private-password"), BodyTooLargeError()],
)
def test_admin_failure_never_prints_password(
    answer: tuple[int, bytes] | Exception,
) -> None:
    fs, engine, _, run = instance_b()
    password: bytes = fs.read_bytes(HOST / "secrets/grafana_admin_password")
    assert isinstance(run.system.admin_http, Admin)
    run.system.admin_http.answer = answer
    with pytest.raises(command.SmokeError, match=r"^4: recovery-login-status$"):
        stages.admin_login(run)
    assert password.decode().strip() not in str(engine.calls)
    assert password.decode().strip() not in str(run.transcript)


@pytest.mark.parametrize(
    "answer",
    [(500, b"private-body"), TransportError("private-error"), BodyTooLargeError()],
)
def test_http_failures_are_named(answer: tuple[int, bytes] | Exception) -> None:
    _, _, http, run = instance_b()
    http.overrides["/api/org"] = answer
    with pytest.raises(command.SmokeError, match=r"^4: grafana-permission-status$"):
        stages.group4(run)
    with pytest.raises(command.SmokeError, match=r"^4: grafana-query-unavailable$"):
        stages.get(run, "/api/org")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_group5_receipt_and_codex(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, http, run = instance_b()
    run.system = replace(run.system, platform=platform)

    # Keep data utility semantics while testing group 5's network selection.
    def skip_disk(_phase: str) -> None:
        pass

    monkeypatch.setattr(run, "record_loki_disk", skip_disk)
    stages.group5(run)
    assert fs.read_text(OUT / "receipt").split() == [
        "01" * 8,
        "01" * 16,
        "16843009",
        "1000",
    ]
    argv, env, timeout = next(
        call for call in engine.calls if "telemetrygen:pinned" in call[0]
    )
    network = ["--network", "host"] if platform == "linux" else []
    desktop_domain = "docker.internal"
    endpoint = (
        "127.0.0.1:14317" if platform == "linux" else f"host.{desktop_domain}:14317"
    )
    assert argv == [
        "docker",
        "run",
        "--rm",
        *network,
        "telemetrygen:pinned",
        "traces",
        "--otlp-endpoint",
        endpoint,
        "--otlp-insecure",
        "--traces",
        "1",
        "--service",
        "o11y-check-grpc-" + "01" * 8,
    ]
    assert timeout == 120
    assert env == run.env
    assert len(http.fixtures) == 3
    assert sum(line.startswith("ok Codex filter:") for line in run.transcript) == 1
    assert run.transcript[-1] == (
        "ok Codex filter: exact span names, IDs, parents and attributes\n"
    )


@pytest.mark.parametrize(
    "answer",
    [(500, b"private-body"), TransportError("private-error"), BodyTooLargeError()],
)
def test_codex_failed_post(answer: tuple[int, bytes] | Exception) -> None:
    _, _, http, run = instance_b()
    http.overrides["/v1/traces"] = answer
    with pytest.raises(command.SmokeError, match=r"^5: codex-filter-post$"):
        stages.codex_span_filter(run)


def test_codex_warmup_and_timeout() -> None:
    _, _, http, run = instance_b()
    http.pending = 1
    stages.codex_span_filter(run)
    assert run.system.monotonic() == 2
    http.overrides["/api/traces/"] = (200, b"{}")
    with pytest.raises(command.SmokeError, match=r"^5: codex-filter-spans$"):
        stages.codex_span_filter(run)
    assert run.system.monotonic() == 62


@pytest.mark.parametrize(
    "response",
    [
        "{}",
        '{"partialSuccess":{}}',
        '{"partialSuccess":{"rejectedDataPoints":0}}',
        '{"partialSuccess":{"rejectedDataPoints":"0"}}',
        '{"partial_success":{}}',
        '{"partial_success":{"rejected_data_points":0}}',
        '{"partial_success":{"rejected_data_points":"0"}}',
    ],
)
def test_export_upload_accepts_zero(response: str) -> None:
    fs, engine, http, run = instance_b()
    engine.upload = response + "\n200"
    before = len(engine.calls)
    stages.export_persistence(run)
    payload = json.loads(fs.read_bytes(OUT / "export-fixtures.json"))
    assert len(payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]) == 49
    assert ("write", OUT / "export-fixtures.json", 0o600) in fs.calls
    assert any(call[1].endswith("time=-800") for call in http.calls)
    query = parse_qs(urlsplit(http.calls[-1][1]).query)
    assert query == {
        "query": ["production"],
        "start": ["-920"],
        "end": ["-800"],
        "step": ["30"],
    }
    argv, _, timeout = next(call for call in engine.calls if "--data-binary" in call[0])
    assert argv[argv.index("exec") : argv.index("curl") + 1] == [
        "exec",
        "-T",
        "rustfs",
        "curl",
    ]
    assert argv[-3:] == [
        "--data-binary",
        "@-",
        "http://mimir:8080/otlp/v1/metrics",
    ]
    assert not any("Authorization" in arg for arg in argv)
    assert argv[argv.index("-m") + 1] == "15"
    assert timeout == 20
    assert engine.inputs[-1] == fs.read_text(OUT / "export-fixtures.json")
    assert not any("cp" in call[0] for call in engine.calls[before:])


def test_every_inside_call_leaves_time_for_exec() -> None:
    _, engine, _, run = instance_b()
    run.group3()
    stages.group4(run)
    stages.group5(run)
    stages.export_persistence(run)
    stages.group6(run)
    limits = set()
    for argv, _, timeout in engine.calls:
        if "curl" in argv:
            curl_limit = int(argv[argv.index("-m") + 1])
            assert timeout > curl_limit
            assert timeout - curl_limit == 5
            limits.add((curl_limit, timeout))
    assert limits == {(10, 15), (15, 20)}


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        "null",
        "[]",
        '{"partialSuccess":{"rejectedDataPoints":1}}',
        '{"partial_success":{"rejected_data_points":"7","error_message":"private-body"}}',
    ],
)
def test_export_rejection_before_query_and_safe_fail(response: str) -> None:
    _, engine, http, run = instance_b()
    engine.upload = response + "\n200"
    with pytest.raises(
        command.SmokeError, match=r"^export persistence: rejected-points$"
    ) as failure:
        stages.export_persistence(run)
    assert "private-body" not in "FAIL " + str(failure.value)
    assert not any("/api/v1/query" in call[1] for call in http.calls)


@pytest.mark.parametrize(
    ("match", "answer", "rule"),
    [
        (
            "http://mimir:8080/otlp/v1/metrics",
            Result(22, "private-body\n200"),
            "upload-exec",
        ),
        (
            "http://mimir:8080/otlp/v1/metrics",
            Result(0, "private-body\n500"),
            "upload-http",
        ),
        ("http://mimir:8080/otlp/v1/metrics", Result(0, "200"), "upload-http"),
    ],
)
def test_export_runner_failures(match: str, answer: Result, rule: str) -> None:
    _, engine, _, run = instance_b()
    engine.overrides[match] = answer
    with pytest.raises(command.SmokeError, match=r"^export persistence: " + rule + "$"):
        stages.export_persistence(run)


@pytest.mark.parametrize(
    ("match", "body", "rule"),
    [
        ("/alert-rules/o11y-alloy-export", b"{}", "production-rule"),
        ("/api/v1/query?", b"private-body", "fixture-count"),
        ("/api/v1/query_range?", b"private-body", "sustained-loss"),
    ],
)
def test_mimir_failures_name_rule_without_body(
    match: str, body: bytes, rule: str
) -> None:
    _, _, http, run = instance_b()
    http.overrides[match] = (200, body)
    with pytest.raises(command.SmokeError) as failure:
        stages.export_persistence(run)
    assert "FAIL " + str(failure.value) == "FAIL export persistence: " + rule
    assert body.decode() not in str(failure.value)


def test_group6_buckets_local_erasure_and_restart() -> None:
    fs, engine, _, run = instance_b()
    stages.group5(run)
    fs.write_atomic(HOST / "loki/.hidden", "local", 0o600)
    engine.counts = {"loki": 1, "mimir-blocks": 2, "tempo": 3}
    stages.group6(run)
    assert run.system.monotonic() == 12
    assert fs.lstat(HOST / "loki/.hidden") is None
    assert fs.lstat(HOST / "loki") is not None
    assert [argv[-1] for argv, _, _ in engine.calls if "restart" in argv] == [
        "tempo",
        "mimir",
    ]
    assert run.transcript[-1].startswith("ok 6:")


@pytest.mark.parametrize(
    ("match", "answer", "rule"),
    [
        ("http://loki:3100/flush", Result(0, "500"), "flush-status"),
        ("http://mimir:8080/ingester/flush", Result(17, "204"), "flush-status"),
        ("rm -sf loki", Result(1), "loki-remove"),
        ("--entrypoint find", Result(1), "loki-data-clear"),
        ("up -d loki", Result(1), "loki-create"),
        ("restart tempo", Result(1), "backend-restart"),
        ("restart mimir", Result(1), "backend-restart"),
    ],
)
def test_persistence_command_failures(match: str, answer: Result, rule: str) -> None:
    _, engine, _, run = instance_b()
    stages.group5(run)
    engine.overrides[match] = answer
    with pytest.raises(command.SmokeError, match=r"^6: " + rule + "$"):
        stages.group6(run)


@pytest.mark.parametrize(
    ("bucket", "timeout", "answer"),
    [
        ("loki", 120, Result(0, "0")),
        ("mimir-blocks", 120, Result(17, "1")),
        ("tempo", 240, Result(0, "private-body")),
    ],
)
def test_bucket_poll_deadlines(bucket: str, timeout: int, answer: Result) -> None:
    _, engine, _, run = instance_b()
    engine.overrides["s3probe count " + bucket] = answer
    with pytest.raises(command.SmokeError, match=r"^6: bucket-empty$"):
        stages.group6(run)
    assert run.system.monotonic() == timeout


def test_recheck_failure() -> None:
    _, _, http, run = instance_b()
    stages.group5(run)
    http.overrides["/loki/"] = (500, b"private-body")
    with pytest.raises(command.SmokeError, match=r"^6: receipt-recheck$"):
        stages.recheck(run)


def test_full_flow_and_mimir_failure_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    for fail in (False, True):
        fs, _engine, http, system = setup_b()
        original_group4 = stages.group4

        def group4(
            run: command.Smoke,
            http: Http = http,
            original_group4: Callable[[command.Smoke], None] = original_group4,
        ) -> None:
            http.webhook = dict(run.sentinels.values)[init.WEBHOOK].decode()
            original_group4(run)

        with monkeypatch.context() as patch:
            patch.setattr(stages, "group4", group4)
            if fail:
                http.overrides["/uid/mimir/api/v1/query_range?"] = (
                    200,
                    b"private-body",
                )
            code, output, error = execute(system)
        assert code == int(fail), output + error
        assert error == ""
        assert "private-body" not in output
        assert ("FAIL export persistence: sustained-loss" in output) == fail, output
        assert ("ok 6:" in output) != fail
        assert fs.lstat(OUT) is fs.lstat(HOST) is None


@pytest.mark.parametrize("group", [5, 6])
def test_receipt_failure_transcript_order(
    group: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, http, system = setup_b()
    original_group4 = stages.group4

    def group4(run: command.Smoke) -> None:
        http.webhook = dict(run.sentinels.values)[init.WEBHOOK].decode()
        original_group4(run)

    monkeypatch.setattr(stages, "group4", group4)
    if group == 5:
        http.overrides["/v1/logs"] = (500, b"private-body")
        rule = "5: receipt"
    else:
        original_group6 = stages.group6

        def group6(run: command.Smoke) -> None:
            http.overrides["/loki/"] = (500, b"private-body")
            original_group6(run)

        monkeypatch.setattr(stages, "group6", group6)
        rule = "6: receipt-recheck"
    code, output, error = execute(system)
    assert code == 1
    assert error == ""
    reason = "signal-check"
    assert f"FAIL receipt: {reason}\nFAIL {rule}\n" in output
    assert "private-body" not in output


def test_admin_desktop_and_file_ownership_failures() -> None:
    fs, engine, _, run = instance_b()
    run.system = replace(run.system, platform="darwin")
    stages.admin_login(run)
    run.system = replace(run.system, platform="linux")
    engine.overrides["123:456 /d/secrets/grafana_admin_password"] = Result(1)
    with pytest.raises(command.SmokeError, match=r"^4: recovery-file-owner$"):
        stages.admin_login(run)
    engine.overrides.clear()
    engine.overrides["472:472 /d/secrets/grafana_admin_password"] = Result(1)
    with pytest.raises(command.SmokeError, match=r"^4: recovery-file-restore$"):
        stages.admin_login(run)
    assert fs.read_bytes(HOST / "secrets/grafana_admin_password")


def stage_contract(stage: str) -> Document:
    _, engine, http, run = instance_b()
    if stage == "group6":
        stages.group5(run)
    before_commands, before_http, before_lines = (
        len(engine.calls),
        len(http.calls),
        len(run.transcript),
    )
    getattr(stages, stage)(run)
    return {
        "commands": [
            [argv, timeout] for argv, _, timeout in engine.calls[before_commands:]
        ],
        "http": [
            [method, url, body.decode() if body is not None else None, headers, timeout]
            for method, url, body, headers, timeout in http.calls[before_http:]
        ],
        "transcript": run.transcript[before_lines:],
    }


@pytest.mark.parametrize("stage", ["group4", "group5", "export_persistence", "group6"])
def test_stage_protocol_contract(stage: str) -> None:
    path = Path(__file__).parent / "testdata" / f"smoke-b-{stage}.json"
    assert stage_contract(stage) == json.loads(path.read_text())


@pytest.mark.parametrize(
    ("classifier", "rule"),
    [
        ("user", "proxy-user"),
        ("orgs", "proxy-org-role"),
        ("owner_model", "owner-service-model"),
        ("whitelist", "proxy-whitelist"),
        ("identifiers", "datasource-identifiers"),
        ("dashboard_checkpoint", "dashboard-checkpoint"),
        ("contact_points", "contact-point-redaction"),
        ("decrypted", "owner-contact-point-export"),
        ("home", "home-preference"),
    ],
)
def test_group4_classifier_failure_rules(
    classifier: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, run = instance_b()

    def reject(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(access, classifier, reject)
    with pytest.raises(command.SmokeError, match=r"^4: " + rule + "$"):
        stages.group4(run)


@pytest.mark.parametrize("meta", [None, [], "malformed", 42, True])
def test_non_mapping_dashboard_meta_fails_rule(meta: object) -> None:
    _, _, http, run = instance_b()
    uid = access.AGENTS[0]
    http.overrides[f"/api/dashboards/uid/{uid}"] = (
        200,
        packed({"dashboard": {"uid": uid}, "meta": meta}),
    )
    with pytest.raises(command.SmokeError, match=r"^4: dashboard-checkpoint$"):
        stages.group4(run)


@pytest.mark.parametrize("service", ["loki", "tempo", "mimir"])
@pytest.mark.parametrize("answer", [Result(26, "200"), Result(0, "503")])
def test_group6_readiness_names_group(service: str, answer: Result) -> None:
    _, engine, _, run = instance_b()
    stages.group5(run)
    engine.overrides[smoke.READY[service]] = answer
    with pytest.raises(
        command.SmokeError, match=rf"^6: {service} did not become ready$"
    ):
        stages.group6(run)
    assert run.system.monotonic() == command.READY_TIMEOUT


def test_codex_success_only_after_all_services_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, http, run = instance_b()
    original = http.post
    posts = 0

    def post(url: str, body: bytes | None) -> tuple[int, bytes]:
        nonlocal posts
        posts += 1
        if posts == 3:
            return 500, b"private-body"
        return original(url, body)

    monkeypatch.setattr(http, "post", post)
    with pytest.raises(command.SmokeError, match=r"^5: codex-filter-post$"):
        stages.codex_span_filter(run)
    assert not any(line.startswith("ok Codex filter:") for line in run.transcript)


@pytest.mark.parametrize(
    ("match", "body", "rule"),
    [
        ("/api/v1/provisioning/alert-rules", b"[]", "alert-rule-identifiers"),
        ("/api/dashboards/home", b"{}", "anonymous-home"),
    ],
)
def test_group4_remaining_http_rules(match: str, body: bytes, rule: str) -> None:
    _, _, http, run = instance_b()
    http.overrides[match] = (200, body)
    with pytest.raises(command.SmokeError, match=r"^4: " + rule + "$"):
        stages.group4(run)


@pytest.mark.parametrize(
    ("match", "result", "rule"),
    [
        (
            "{{.State.Status}} {{.State.ExitCode}}",
            Result(17, "exited 0"),
            "owner-service-exit",
        ),
        (
            "{{.State.Status}} {{.State.ExitCode}}",
            Result(0, "running 0"),
            "owner-service-exit",
        ),
        ("GF_AUTH_PROXY_WHITELIST", Result(17, "127.0.0.1"), "proxy-whitelist"),
        ("http://grafana:3000/api/user", Result(0, "200"), "peer-proxy-permission"),
        ("http://grafana:3000/api/user", Result(17, "401"), "peer-proxy-permission"),
        (
            "logs --no-log-prefix grafana",
            Result(17, "provisioning"),
            "provisioning-log-unavailable",
        ),
        (
            "logs --no-log-prefix grafana",
            Result(0, "provisioning level=error private-body"),
            "provisioning-log",
        ),
    ],
)
def test_group4_process_failure_rules(match: str, result: Result, rule: str) -> None:
    _, engine, _, run = instance_b()
    engine.overrides[match] = result
    with pytest.raises(command.SmokeError, match=r"^4: " + rule + "$"):
        stages.group4(run)


def test_proxy_recovery_contract() -> None:
    _, _, http, run = instance_b()
    stages.proxy_role(run, "admin", "Admin", recovery=True)
    assert [call[3] for call in http.calls[-2:]] == [
        {"Tailscale-User-Login": "admin"}
    ] * 2
    http.overrides["/api/user"] = (200, b'{"login":"admin","isGrafanaAdmin":false}')
    with pytest.raises(command.SmokeError, match=r"^4: proxy-user$"):
        stages.proxy_role(run, "admin", "Admin", recovery=True)


def test_request_seam_contract() -> None:
    _, _, http, run = instance_b()
    stages.request(run, "/api/org", login="viewer", method="POST", data=b"payload")
    assert http.calls[-1] == (
        "POST",
        "http://127.0.0.1:13000/api/org",
        b"payload",
        {"Tailscale-User-Login": "viewer", "Content-Type": "application/json"},
        15,
    )
    for error in (BodyTooLargeError(), TransportError("private-body")):
        http.overrides["/api/org"] = error
        assert stages.request(run, "/api/org") == (0, b"")


def test_group5_missing_image_and_failed_receipt() -> None:
    _, _, http, run = instance_b()
    del run.model["telemetrygen"]
    with pytest.raises(command.SmokeError, match=r"^5: telemetrygen-image$"):
        stages.group5(run)
    run.model["telemetrygen"] = {"image": "telemetrygen:pinned"}
    http.overrides["/v1/logs"] = (500, b"private-body")
    with pytest.raises(command.SmokeError, match=r"^5: receipt$"):
        stages.group5(run)


def test_recovery_keeps_stored_org_role() -> None:
    _, _, http, run = instance_b()
    http.overrides["/api/user/orgs"] = (200, b'[{"role":"Admin"}]')
    stages.proxy_role(run, "admin", "Admin", recovery=True)
    with pytest.raises(command.SmokeError, match=r"^4: proxy-org-role$"):
        stages.proxy_role(run, access.OWNER, "Admin")


@pytest.mark.parametrize(
    ("text", "passes"), [("401\n", True), ("401 ", False), ("401X", False)]
)
def test_peer_status_strips_only_trailing_newline(text: str, passes: bool) -> None:
    _, engine, _, run = instance_b()
    engine.overrides["http://grafana:3000/api/user"] = Result(0, text)
    if passes:
        stages.group4(run)
    else:
        with pytest.raises(command.SmokeError, match=r"^4: peer-proxy-permission$"):
            stages.group4(run)


@pytest.mark.parametrize(
    ("text", "passes"), [("204\n", True), ("204 ", False), ("204X", False)]
)
def test_flush_status_strips_only_trailing_newline(text: str, passes: bool) -> None:
    _, engine, _, run = instance_b()
    stages.group5(run)
    engine.overrides["http://loki:3100/flush"] = Result(0, text)
    if passes:
        stages.group6(run)
    else:
        with pytest.raises(command.SmokeError, match=r"^6: flush-status$"):
            stages.group6(run)


@pytest.mark.parametrize(
    ("response", "passes"),
    [
        ("{}\n200 ", False),
        ("{}\n200X", False),
        ('{\n"partialSuccess":{}\n}\n200\n', True),
    ],
)
def test_upload_status_and_multiline_response(response: str, passes: bool) -> None:
    _, engine, _, run = instance_b()
    engine.upload = response
    if passes:
        stages.export_persistence(run)
    else:
        with pytest.raises(
            command.SmokeError, match=r"^export persistence: upload-http$"
        ):
            stages.export_persistence(run)


def test_export_elapsed_clock() -> None:
    _, _, _, run = instance_b()
    ticks = iter([11, 18])
    run.system = replace(run.system, monotonic=lambda: next(ticks))
    stages.export_persistence(run)
    assert run.transcript[-1].endswith("(7 seconds)\n")


@pytest.mark.parametrize("dedicated", [None, "unix:///run/dind/docker.sock"])
def test_group5_dispatch_engine_environment(
    dedicated: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, http, system = setup_b()
    original_group4 = stages.group4
    original_finalize = command.Smoke.finalize_transcript
    finalized = []

    def finalize(run: command.Smoke) -> None:
        original_finalize(run)
        finalized.append(fs.read_text(OUT / "transcript.log"))

    def group4(run: command.Smoke) -> None:
        http.webhook = dict(run.sentinels.values)[init.WEBHOOK].decode()
        original_group4(run)

    monkeypatch.setattr(stages, "group4", group4)
    monkeypatch.setattr(command.Smoke, "finalize_transcript", finalize)
    if dedicated is not None:
        fs.modes[Path("/run/dind/docker.sock")] = stat.S_IFSOCK | 0o600
        engine.overrides.update(
            {
                "context inspect": Result(0, dedicated),
                "ps -aq": Result(0),
                "volume ls -q": Result(0),
                "network ls --format": Result(0, "bridge\nhost\nnone\n"),
            }
        )
    args = ["--dedicated-engine", dedicated] if dedicated is not None else []
    code, output, error = execute(system, *args)
    assert code == 0, output
    assert error == ""
    assert finalized == [output, output]
    assert fs.lstat(OUT) is fs.lstat(HOST) is None
    assert any("down" in argv for argv, _, _ in engine.calls)
    calls = [call for call in engine.calls if "telemetrygen:pinned" in call[0]]
    assert len(calls) == 1
    argv, env, _ = calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "ok 5: receipt" in output
    assert "AWS_SECRET_ACCESS_KEY" not in env
    if dedicated is None:
        assert "DOCKER_HOST" not in env
    else:
        assert env["DOCKER_HOST"] == dedicated


def test_group6_replaces_up_log() -> None:
    fs, _, _, run = instance_b()
    stages.group5(run)
    fs.write_atomic(OUT / "up.log", "old", 0o600)
    stages.group6(run)
    assert fs.read_text(OUT / "up.log") == "started"


@pytest.mark.parametrize(
    "answer",
    [(500, b"private-body"), BodyTooLargeError(), TransportError("private-body")],
)
def test_history_query_http_failure(answer: tuple[int, bytes] | Exception) -> None:
    _, _, http, run = instance_b()
    http.overrides["/uid/mimir/api/v1/query_range?"] = answer
    with pytest.raises(command.SmokeError, match=r"^export persistence: query-http$"):
        stages.export_persistence(run)


def test_history_query_success_contract() -> None:
    _, _, http, run = instance_b()
    query = "query=production&start=100&end=220&step=30"
    body = stages.history_query(run, query)
    assert json.loads(body)["status"] == "success"
    assert http.calls[-1] == (
        "GET",
        "http://127.0.0.1:13000/api/datasources/proxy/uid/mimir/api/v1/query_range?"
        + query,
        None,
        {},
        30,
    )


def test_receipt_refusal_after_successful_write_probe() -> None:
    _, engine, http, run = instance_b()
    http.overrides["/uid/loki/"] = (200, b'{"status":"success","data":{"result":[]}}')
    with pytest.raises(command.SmokeError, match=r"^5: receipt$"):
        stages.group5(run)
    probes = [
        argv
        for argv, _, _ in engine.calls
        if argv[-1] == "http://loki:3100/loki/api/v1/push"
    ]
    assert len(probes) == 1
    assert json.loads(probes[0][probes[0].index("-d") + 1])["streams"][0]["stream"] == {
        "service_name": "o11y-smoke-probe"
    }
    assert run.system.monotonic() == 60
    assert not any("probe" in call[1] for call in http.calls)


def test_grafana_logs_are_private_and_scanned_for_password() -> None:
    fs, engine, _, run = instance_b()
    password: bytes = fs.read_bytes(HOST / "secrets/grafana_admin_password")
    engine.overrides["logs --no-log-prefix grafana"] = Result(
        0, "provisioning level=info " + password.decode()
    )
    stages.group4(run)
    assert fs.read_bytes(OUT / "grafana.log").endswith(password)
    assert ("write", OUT / "grafana.log", 0o600) in fs.calls
    run.finalize_transcript()
    with pytest.raises(
        command.SmokeError, match=r"^sentinel secret: grafana.log$"
    ) as failure:
        run.scans()
    assert password.decode().strip() not in str(failure.value)
    assert password.decode().strip() not in "".join(run.transcript)
    assert password.decode().strip() not in str(engine.calls)
