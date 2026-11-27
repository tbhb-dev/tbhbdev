"""Smoke self-monitoring and RustFS metrics."""

import json
import re

import pytest
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    monitoring,
    queries,
    runtime,
    safety,
)
from unit.o11y.smoke.grafana_fixture import packed
from unit.o11y.smoke.metrics_fixture import RULES, vector
from unit.o11y.smoke.stack_fixture import SOURCE, instance_c
from unit.o11y.smoke.stage_contracts import (
    check_http_failure,
    check_process_failure,
    check_stage_protocol,
    fault_cases,
)
lazy from tbhbops.o11y.validate import Document

STAGES = ("group7",)


def test_group7_self_monitoring() -> None:
    _, engine, _, run = instance_c()
    monitoring.group7(run, SOURCE)
    assert engine.stopped == set()
    assert run.transcript[-1].startswith("ok 7:")


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
    assert monitoring.recent(values) is fresh
    assert monitoring.recent(values, stale=True) is stale


@pytest.mark.parametrize("value", ["bad", None])
def test_monitoring_invalid_numbers(value: str) -> None:
    _, _, http, run = instance_c()
    http.overrides["/api/v1/query?"] = (
        200,
        packed({"status": "success", "data": {"result": [{"value": [0, value]}]}}),
    )
    with pytest.raises(safety.SmokeError, match=r"^7: scrape-jobs-up$"):
        monitoring.group7(run, SOURCE)


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
    with pytest.raises(safety.SmokeError, match=r"^7: refused-counter-samples$"):
        monitoring.group7(run, SOURCE)
    http.overrides["/api/v1/series?"] = (200, b"null")
    with pytest.raises(safety.SmokeError, match=r"^7: rustfs-metric-allowlist$"):
        monitoring.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))


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
    original = queries.evaluate

    def evaluate(
        run: runtime.Smoke, uid: str, threshold: int | None = None
    ) -> list[Document] | None:
        rows = original(run, uid, threshold)
        if uid == "o11y-service-down":
            assert rows is not None
            if mode == "missing-job":
                return rows[:-1]
            rows[0]["values"] = [1]
        return rows

    monkeypatch.setattr(queries, "evaluate", evaluate)
    with pytest.raises(safety.SmokeError, match=r"^7: service-down-coverage$"):
        monitoring.group7(run, SOURCE)


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
    with pytest.raises(safety.SmokeError, match="^7: " + rule + "$"):
        monitoring.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))


def test_rustfs_allowlist_ignores_comment_prototypes() -> None:
    _, _, _, run = instance_c()
    text = run.fs.read_text(SOURCE / "config/alloy/config.alloy")
    marker = 'otelcol.processor.filter "self" {'
    text = text.replace(marker, marker + "\n// " + marker)
    end = text.index("\n}", text.index(marker))
    text = text[:end] + "\n// " + marker + text[end:]
    monitoring.rustfs(run, text)


def test_rustfs_allowlist_excludes_later_comments() -> None:
    _, _, http, run = instance_c()
    text = run.fs.read_text(SOURCE / "config/alloy/config.alloy")
    end = text.index("\n}", text.index('otelcol.processor.filter "self" {')) + 2
    text = text[:end] + '\n// "rustfs_forbidden_metric"\n' + text[end:]
    http.overrides["/api/v1/series?"] = (
        200,
        b'{"data":[{"__name__":"rustfs_forbidden_metric"}]}',
    )
    with pytest.raises(safety.SmokeError, match=r"^7: rustfs-metric-allowlist$"):
        monitoring.rustfs(run, text)


@pytest.mark.parametrize(
    ("mode", "rule"),
    [("stopped-rule", "stopped-tempo-rule"), ("recovery", "recovered-scrape-jobs")],
)
def test_monitoring_later_rule_failures(
    mode: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, http, run = instance_c()
    original = queries.evaluate

    def evaluate(
        run: runtime.Smoke, uid: str, threshold: int | None = None
    ) -> list[Document] | None:
        if uid == "o11y-service-down" and "tempo" in engine.stopped:
            return [{"labels": {}, "values": [0]}]
        return original(run, uid, threshold)

    if mode == "stopped-rule":
        monkeypatch.setattr(queries, "evaluate", evaluate)
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
    with pytest.raises(safety.SmokeError, match="^7: " + rule + "$"):
        monitoring.group7(run, SOURCE)


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
            monitoring.group7(run, SOURCE)
        else:
            monitoring.rustfs(run, config)

    with pytest.raises(safety.SmokeError, match="^7: " + rule + "$"):
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
    monitoring.rustfs(run, config)


def test_rustfs_start_line_can_follow_prelude_and_use_crlf() -> None:
    _, engine, _, run = instance_c()
    engine.overrides["logs --no-log-prefix rustfs"] = Result(
        0, 'entrypoint prelude\r\nStarting: rustfs\r\n{"console_enabled":false}\r\n'
    )
    monitoring.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))
    engine.overrides["logs --no-log-prefix rustfs"] = Result(
        0,
        "entrypoint prelude\nStarting: rustfs\nexporter "
        'dump\n{"console_enabled":false}\n',
    )
    with pytest.raises(safety.SmokeError, match=r"^7: rustfs-json-after-start$"):
        monitoring.rustfs(run, run.fs.read_text(SOURCE / "config/alloy/config.alloy"))


@pytest.mark.parametrize("stage", STAGES)
def test_stage_protocol_contract(stage: str) -> None:
    check_stage_protocol(stage)


@pytest.mark.parametrize(("stage", "match"), fault_cases("commands", STAGES))
def test_every_process_failure_names_a_rule(stage: str, match: str) -> None:
    check_process_failure(stage, match)


@pytest.mark.parametrize(("stage", "match"), fault_cases("http", STAGES))
def test_every_http_failure_names_a_rule(stage: str, match: str) -> None:
    check_http_failure(stage, match)
