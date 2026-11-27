"""Capacity telemetry verification and cleanup."""

import json
from functools import partial
from pathlib import Path
lazy from collections.abc import Callable

import pytest
import yaml
from tbhbops.o11y import smoke_capacity as capacity
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.smoke import capacity as cap
from tbhbops_cli.o11y.smoke import (
    lifecycle,
    queries,
    runtime,
    safety,
)
from unit.o11y.smoke.grafana_fixture import packed
from unit.o11y.smoke.metrics_fixture import RULES, HttpC, windows
from unit.o11y.smoke.runtime_fixture import OUT
from unit.o11y.smoke.stack_fixture import SOURCE, AdminC, instance_c
from unit.o11y.smoke.stage_contracts import (
    check_http_failure,
    check_process_failure,
    check_stage_protocol,
    fault_cases,
)
lazy from tbhbops.o11y.validate import Document

STAGES = ("smoke", "cleanup")


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
        safety.SmokeError, match=r"^capacity cleanup: provisioning-reload$"
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

    def delayed(run: runtime.Smoke, title: str, now: int) -> list[Document] | None:
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
    with pytest.raises(safety.SmokeError, match="^capacity: " + rule + "$"):
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
        http.overrides[queries.RULES_PATH] = (200, b"{}")
    else:
        original = cap.panel

        def panel(run: runtime.Smoke, title: str, now: int) -> list[Document] | None:
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
    with pytest.raises(safety.SmokeError, match="^capacity: " + rule + "$"):
        cap.smoke(run, SOURCE)


def test_capacity_populated_condition_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, run = instance_c()

    def missing(*_args: object) -> bool:
        return False

    monkeypatch.setattr(queries, "fires", missing)
    with pytest.raises(safety.SmokeError, match=r"^capacity: populated-condition$"):
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
    with pytest.raises(safety.SmokeError, match=r"^capacity cleanup: release$"):
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
    with pytest.raises(safety.SmokeError, match="^capacity cleanup: " + rule + "$"):
        cap.cleanup(run)


def cleanup_read(original: Callable[[Path], str], http: HttpC, path: Path) -> str:
    text = original(path)
    if http.deleted_rules and path.name == "agents.yaml":
        document = yaml.safe_load(text)
        document["providers"][0]["disableDeletion"] = False
        return yaml.safe_dump(document)
    return text


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
    "line", ["private", "=private", "  =private", "bad key=private", "1KEY=private"]
)
def test_cleanup_malformed_env_redacts_value(line: str) -> None:
    fs, _, http, run = instance_c()
    http.email_off = True
    lifecycle.group13(run, SOURCE)
    assert run.release is not None
    fs.write_atomic(run.release / ".env", line, 0o600)
    with pytest.raises(
        safety.SmokeError, match=r"^capacity cleanup: malformed-env-line$"
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

    def reload(run: runtime.Smoke, kind: str) -> None:
        nonlocal count
        count += 1
        if count == 1 and disable_fails:
            message = "capacity cleanup: disable-failed"
            raise safety.SmokeError(message)
        if count == (2 if disable_fails else 3):
            message = "capacity cleanup: restore-failed"
            raise safety.SmokeError(message)
        original(run, kind)

    monkeypatch.setattr(cap, "reload", reload)
    with pytest.raises(
        safety.SmokeError,
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

    def panel(run: runtime.Smoke, title: str, now: int) -> list[Document] | None:
        nonlocal expired
        if now > 2200:
            expired = True
        if expired and title == "Live windows":
            return []
        return original(run, title, now)

    monkeypatch.setattr(cap, "panel", panel)
    with pytest.raises(safety.SmokeError, match=r"^capacity: expired-inventory$"):
        cap.smoke(run, SOURCE)


@pytest.mark.parametrize("stage", STAGES)
def test_stage_protocol_contract(stage: str) -> None:
    check_stage_protocol(stage)


@pytest.mark.parametrize(("stage", "match"), fault_cases("commands", STAGES))
def test_every_process_failure_names_a_rule(stage: str, match: str) -> None:
    check_process_failure(stage, match)


@pytest.mark.parametrize(("stage", "match"), fault_cases("http", STAGES))
def test_every_http_failure_names_a_rule(stage: str, match: str) -> None:
    check_http_failure(stage, match)
