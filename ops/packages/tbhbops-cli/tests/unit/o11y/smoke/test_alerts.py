"""Alert rule evaluation and smoke notification delivery."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import hashlib
import io
import json
import subprocess
from functools import partial
from pathlib import Path

import pytest
import yaml
from tbhbops.o11y import init, smoke
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    alerts,
    diagnostics,
    monitoring,
    queries,
    runtime,
    safety,
    telemetry,
)
from unit.o11y.smoke.metrics_fixture import alert_evaluation, scheduler_rules
from unit.o11y.smoke.stack_fixture import ROOT, SOURCE, instance_c
from unit.o11y.smoke.stage_contracts import (
    check_http_failure,
    check_process_failure,
    check_stage_protocol,
    fault_cases,
)
lazy from tbhbops.o11y.validate import Document

STAGES = ("group12",)


def test_delta_and_alert_delivery() -> None:
    _, engine, http, run = instance_c()
    telemetry.group11(run)
    assert engine.delta == "2"
    alerts.group12(run, SOURCE)
    assert http.old
    assert http.rate
    assert run.transcript[-1].startswith("ok 12:")
    assert not any("smoke diagnostics:" in line for line in run.transcript)


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
    original = queries.evaluate

    monkeypatch.setattr(
        queries, "evaluate", partial(alert_evaluation, (mode, http), original)
    )
    if mode == "health":
        http.overrides[queries.RULES_PATH] = (200, b"{}")
    with pytest.raises(safety.SmokeError, match="^" + rule + "$"):
        alerts.group12(run, SOURCE)


class PastRuleHealthError(Exception):
    pass


@pytest.mark.parametrize("unhealthy_reads", [0, 3])
def test_rule_health_retries_until_healthy(
    unhealthy_reads: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, run = instance_c()
    reads = 0

    def get(_run: runtime.Smoke, path: str, group: str) -> bytes:
        nonlocal reads
        assert (path, group) == (queries.RULES_PATH, "12")
        reads += 1
        return b"{}" if reads <= unhealthy_reads else scheduler_rules()[1]

    def evaluate(*_args: object) -> None:
        raise PastRuleHealthError

    monkeypatch.setattr(queries, "get", get)
    monkeypatch.setattr(queries, "evaluate", evaluate)
    with pytest.raises(PastRuleHealthError):
        alerts.group12(run, SOURCE)
    assert reads == unhealthy_reads + 1
    assert run.system.monotonic() == 2 * unhealthy_reads


def test_rule_health_gives_up_after_timeout() -> None:
    _, _, http, run = instance_c()
    http.overrides[queries.RULES_PATH] = (200, b"{}")
    with pytest.raises(safety.SmokeError, match=r"^12: rule-health$"):
        alerts.group12(run, SOURCE)
    assert run.system.monotonic() == monitoring.RULE_TIMEOUT


def test_rule_health_http_failure_is_not_retried() -> None:
    _, _, http, run = instance_c()
    http.overrides[queries.RULES_PATH] = (503, b"")
    with pytest.raises(safety.SmokeError, match=r"^12: query-http$"):
        alerts.group12(run, SOURCE)
    assert run.system.monotonic() == 0


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
        original = queries.evaluate

        def evaluate(
            run: runtime.Smoke, uid: str, threshold: int | None = None
        ) -> list[Document] | None:
            return (
                []
                if uid == "o11y-log-rate" and not http.rate
                else original(run, uid, threshold)
            )

        monkeypatch.setattr(queries, "evaluate", evaluate)
    elif mode == "rate-upload":
        original_post = telemetry.post

        def post(run: runtime.Smoke, signal: str, body: bytes) -> bool:
            return (
                False
                if b"o11y-smoke-rate" in body
                else original_post(run, signal, body)
            )

        monkeypatch.setattr(telemetry, "post", post)
    elif mode == "webhook":
        engine.overrides["cat " + alerts.WEBHOOK_POSTS] = Result(0, "unrelated")
    else:
        engine.overrides["http://127.0.0.1:8025/api/v1/messages"] = Result(0, "{}")
    with pytest.raises(safety.SmokeError, match="^12: " + rule + "$"):
        alerts.group12(run, SOURCE)


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
    engine.overrides[alerts.MAIL_MESSAGES] = Result(0, json.dumps({"total": count}))
    alerts.smtp_diagnostics(run)
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
        alerts.MAIL_MESSAGES,
    ]
    assert "--profile" in calls[1][0]
    assert [timeout for _, _, timeout in calls] == [120, 120]


@pytest.mark.parametrize("target", ["logs --no-color grafana", alerts.MAIL_MESSAGES])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_smtp_diagnostics_search_raw_captures(target: str, stream: str) -> None:
    fs, engine, _, run = instance_c()
    for needle in run.sentinels.scan_values():
        raw = "unrelated " + needle.decode()
        engine.overrides["logs --no-color grafana"] = Result(0, "smtp error\n")
        engine.overrides[alerts.MAIL_MESSAGES] = Result(0, '{"total":0}')
        engine.overrides[target] = (
            Result(0, raw) if stream == "stdout" else Result(0, "safe", raw)
        )
        with pytest.raises(
            safety.SmokeError, match=r"^diagnostics: SMTP capture unsafe$"
        ):
            alerts.smtp_diagnostics(run)
        assert not any(run.artifacts in path.parents for path in fs.files)


@pytest.mark.parametrize("target", ["logs --no-color grafana", alerts.MAIL_MESSAGES])
@pytest.mark.parametrize("result", [Result(1, "failed"), Result(0)])
def test_smtp_diagnostics_refuse_failed_or_empty_capture(
    target: str, result: Result
) -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[alerts.MAIL_MESSAGES] = Result(0, '{"total":0}')
    engine.overrides[target] = result
    with pytest.raises(safety.SmokeError, match=r"^diagnostics: SMTP capture "):
        alerts.smtp_diagnostics(run)
    assert not any(run.artifacts in path.parents for path in fs.files)


@pytest.mark.parametrize(
    "body", ["{}", "[]", '{"total":true}', '{"total":-1}', '{"total":"0"}', chr(0xDCFF)]
)
def test_smtp_diagnostics_refuse_invalid_count(body: str) -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[alerts.MAIL_MESSAGES] = Result(0, body)
    with pytest.raises(
        safety.SmokeError, match=r"^diagnostics: mail count unavailable$"
    ):
        alerts.smtp_diagnostics(run)
    assert not any(run.artifacts in path.parents for path in fs.files)


def test_smtp_failure_keeps_delivery_error_when_capture_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[alerts.MAIL_MESSAGES] = Result(0, '{"total":0}')

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(fs, "write_atomic", denied)
    with pytest.raises(safety.SmokeError, match=r"^12: smtp-delivery$"):
        alerts.group12(run, SOURCE)
    assert run.transcript[-1] == "smoke diagnostics: SMTP capture or scan unavailable\n"


def test_smtp_failure_evidence_survives_finish_with_backend_logs() -> None:
    fs, engine, _, run = instance_c()
    engine.overrides[alerts.MAIL_MESSAGES] = Result(0, '{"total":0}')
    engine.overrides["logs --no-color grafana"] = Result(
        0, "smtp authentication failed\n"
    )
    with pytest.raises(safety.SmokeError, match=r"^12: smtp-delivery$"):
        alerts.group12(run, SOURCE)
    # finish must append its normal backend diagnostics to the stage's evidence.
    run.emit("FAIL 12: smtp-delivery")
    output = io.StringIO()
    diagnostics.finish(run, 1, output)
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
    original = queries.evaluate

    def evaluate(
        run: runtime.Smoke, rule: str, limit: int | None = None
    ) -> list[Document] | None:
        if (rule, limit) == ("o11y-" + uid, threshold):
            return rows
        return original(run, rule, limit)

    monkeypatch.setattr(queries, "evaluate", evaluate)
    with pytest.raises(safety.SmokeError, match=r"^12: baseline-conditions$"):
        alerts.group12(run, SOURCE)
    assert run.transcript == [
        (
            f"smoke diagnostics: rule=o11y-{uid} threshold={threshold} "
            f"expected={expected} observed={observed}\n"
        )
    ]


@pytest.mark.parametrize("stage", STAGES)
def test_stage_protocol_contract(stage: str) -> None:
    check_stage_protocol(stage)


@pytest.mark.parametrize(("stage", "match"), fault_cases("commands", STAGES))
def test_every_process_failure_names_a_rule(stage: str, match: str) -> None:
    check_process_failure(stage, match)


@pytest.mark.parametrize(("stage", "match"), fault_cases("http", STAGES))
def test_every_http_failure_names_a_rule(stage: str, match: str) -> None:
    check_http_failure(stage, match)
