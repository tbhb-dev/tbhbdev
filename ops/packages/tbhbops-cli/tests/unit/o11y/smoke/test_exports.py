"""Smoke receipts, export history, and persistence."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from tbhbops.o11y import smoke
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    exports,
    grafana,
    readiness,
    safety,
)
from unit.o11y.smoke.grafana_fixture import instance_b
from unit.o11y.smoke.runtime_fixture import HOST, OUT
from unit.o11y.smoke.stage_contracts import check_export_protocol


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
    exports.group5(run)
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
    with pytest.raises(safety.SmokeError, match=r"^5: codex-filter-post$"):
        exports.codex_span_filter(run)


def test_codex_warmup_and_timeout() -> None:
    _, _, http, run = instance_b()
    http.pending = 1
    exports.codex_span_filter(run)
    assert run.system.monotonic() == 2
    http.overrides["/api/traces/"] = (200, b"{}")
    with pytest.raises(safety.SmokeError, match=r"^5: codex-filter-spans$"):
        exports.codex_span_filter(run)
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
    exports.export_persistence(run)
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
    readiness.group3(run)
    grafana.group4(run)
    exports.group5(run)
    exports.export_persistence(run)
    exports.group6(run)
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
        safety.SmokeError, match=r"^export persistence: rejected-points$"
    ) as failure:
        exports.export_persistence(run)
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
    with pytest.raises(safety.SmokeError, match=r"^export persistence: " + rule + "$"):
        exports.export_persistence(run)


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
    with pytest.raises(safety.SmokeError) as failure:
        exports.export_persistence(run)
    assert "FAIL " + str(failure.value) == "FAIL export persistence: " + rule
    assert body.decode() not in str(failure.value)


def test_group6_buckets_local_erasure_and_restart() -> None:
    fs, engine, _, run = instance_b()
    exports.group5(run)
    fs.write_atomic(HOST / "loki/.hidden", "local", 0o600)
    engine.counts = {"loki": 1, "mimir-blocks": 2, "tempo": 3}
    exports.group6(run)
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
    exports.group5(run)
    engine.overrides[match] = answer
    with pytest.raises(safety.SmokeError, match=r"^6: " + rule + "$"):
        exports.group6(run)


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
    with pytest.raises(safety.SmokeError, match=r"^6: bucket-empty$"):
        exports.group6(run)
    assert run.system.monotonic() == timeout


def test_recheck_failure() -> None:
    _, _, http, run = instance_b()
    exports.group5(run)
    http.overrides["/loki/"] = (500, b"private-body")
    with pytest.raises(safety.SmokeError, match=r"^6: receipt-recheck$"):
        exports.recheck(run)


@pytest.mark.parametrize("service", ["loki", "tempo", "mimir"])
@pytest.mark.parametrize("answer", [Result(26, "200"), Result(0, "503")])
def test_group6_readiness_names_group(service: str, answer: Result) -> None:
    _, engine, _, run = instance_b()
    exports.group5(run)
    engine.overrides[smoke.READY[service]] = answer
    with pytest.raises(
        safety.SmokeError, match=rf"^6: {service} did not become ready$"
    ):
        exports.group6(run)
    assert run.system.monotonic() == readiness.READY_TIMEOUT


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
    with pytest.raises(safety.SmokeError, match=r"^5: codex-filter-post$"):
        exports.codex_span_filter(run)
    assert not any(line.startswith("ok Codex filter:") for line in run.transcript)


def test_group5_missing_image_and_failed_receipt() -> None:
    _, _, http, run = instance_b()
    del run.model["telemetrygen"]
    with pytest.raises(safety.SmokeError, match=r"^5: telemetrygen-image$"):
        exports.group5(run)
    run.model["telemetrygen"] = {"image": "telemetrygen:pinned"}
    http.overrides["/v1/logs"] = (500, b"private-body")
    with pytest.raises(safety.SmokeError, match=r"^5: receipt$"):
        exports.group5(run)


@pytest.mark.parametrize(
    ("text", "passes"), [("204\n", True), ("204 ", False), ("204X", False)]
)
def test_flush_status_strips_only_trailing_newline(text: str, passes: bool) -> None:
    _, engine, _, run = instance_b()
    exports.group5(run)
    engine.overrides["http://loki:3100/flush"] = Result(0, text)
    if passes:
        exports.group6(run)
    else:
        with pytest.raises(safety.SmokeError, match=r"^6: flush-status$"):
            exports.group6(run)


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
        exports.export_persistence(run)
    else:
        with pytest.raises(
            safety.SmokeError, match=r"^export persistence: upload-http$"
        ):
            exports.export_persistence(run)


def test_export_elapsed_clock() -> None:
    _, _, _, run = instance_b()
    ticks = iter([11, 18])
    run.system = replace(run.system, monotonic=lambda: next(ticks))
    exports.export_persistence(run)
    assert run.transcript[-1].endswith("(7 seconds)\n")


def test_group6_replaces_up_log() -> None:
    fs, _, _, run = instance_b()
    exports.group5(run)
    fs.write_atomic(OUT / "up.log", "old", 0o600)
    exports.group6(run)
    assert fs.read_text(OUT / "up.log") == "started"


@pytest.mark.parametrize(
    "answer",
    [(500, b"private-body"), BodyTooLargeError(), TransportError("private-body")],
)
def test_history_query_http_failure(answer: tuple[int, bytes] | Exception) -> None:
    _, _, http, run = instance_b()
    http.overrides["/uid/mimir/api/v1/query_range?"] = answer
    with pytest.raises(safety.SmokeError, match=r"^export persistence: query-http$"):
        exports.export_persistence(run)


def test_history_query_success_contract() -> None:
    _, _, http, run = instance_b()
    query = "query=production&start=100&end=220&step=30"
    body = exports.history_query(run, query)
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
    with pytest.raises(safety.SmokeError, match=r"^5: receipt$"):
        exports.group5(run)
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


@pytest.mark.parametrize("stage", ("group5", "export_persistence", "group6"))
def test_export_protocol_contract(stage: str) -> None:
    check_export_protocol(stage)
