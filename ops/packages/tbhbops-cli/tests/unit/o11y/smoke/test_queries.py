"""Smoke Grafana queries and helper receipts."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import pytest
from tbhbops_cli.o11y import receipt
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.smoke import capacity as cap
from tbhbops_cli.o11y.smoke import (
    monitoring,
    queries,
    safety,
    transport,
)
from unit.o11y.smoke.grafana_fixture import packed
from unit.o11y.smoke.metrics_fixture import (
    DASHBOARD,
    HEALTH,
    RULES,
    frame,
    table_frames,
    windows,
)
from unit.o11y.smoke.runtime_fixture import OUT
from unit.o11y.smoke.stack_fixture import instance_c


@pytest.mark.parametrize(
    "answer", [(500, b"private"), TransportError("private"), BodyTooLargeError()]
)
def test_query_failure_boundaries(answer: tuple[int, bytes] | Exception) -> None:
    _, _, http, run = instance_c()
    http.overrides["/query"] = answer
    assert transport.request(run, "/query") == (
        (0, b"") if isinstance(answer, Exception) else answer
    )
    with pytest.raises(safety.SmokeError, match=r"^fixture: query-http$"):
        queries.get(run, "/query", "fixture")
    http.overrides = {"/api/v1/query": answer}
    assert queries.mimir(run, "test", time=123) == b""
    assert http.calls[-1][1].endswith("query=test&time=123")
    http.overrides = {"/alert-rules/": answer}
    assert queries.evaluate(run, "o11y-disk-low") is None
    http.overrides = {"/api/dashboards/": answer}
    assert monitoring.panel(run, "test") is None
    assert cap.panel(run, "Live windows", 1000) is None


def test_invalid_eval_and_panel_data() -> None:
    _, _, http, run = instance_c()
    http.overrides["/alert-rules/"] = (200, b"null")
    assert queries.evaluate(run, "o11y-disk-low") is None
    http.overrides = {"/api/v1/eval": (500, b"private")}
    assert queries.evaluate(run, "o11y-disk-low") is None
    http.overrides = {"/api/dashboards/": (200, b"null")}
    assert monitoring.panel(run, "test") is None
    assert cap.panel(run, "Live windows", 1000) is None
    http.overrides = {"/api/ds/query": (500, b"private")}
    assert cap.panel(run, "Live windows", 1000) is None


def test_safe_receipt_failures_preserve_helper_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, http, run = instance_c()
    http.overrides["/v1/traces"] = (500, b"private")
    with pytest.raises(safety.SmokeError, match=r"^7: receipt$"):
        queries.helper_receipt(run, "7", traces=True)
    assert run.transcript[-1] == "FAIL receipt: signal-check\n"
    http.overrides.clear()
    http.overrides["/uid/loki/"] = (200, b'{"status":"success","data":{"result":[]}}')
    with pytest.raises(safety.SmokeError, match=r"^9: receipt$"):
        queries.helper_receipt(run, "9")
    assert run.transcript[-1] == "FAIL receipt: signal-check\n"
    assert not any(line.startswith("FAIL receipt Loki:") for line in run.transcript)

    def denied(*_args: object, **_kwargs: object) -> None:
        message = "private"
        raise OSError(message)

    monkeypatch.setattr(run.fs, "read_text", denied)
    lines, passed = receipt.receipt(
        run.system, None, transport.GRAFANA, recheck=OUT / "receipt", safe_failures=True
    )
    assert not passed
    assert lines == ["FAIL receipt: operation-unavailable"]


def test_failed_http_with_valid_bodies_never_evaluates() -> None:
    _, _, http, run = instance_c()
    definition = next(
        r for g in RULES["groups"] for r in g["rules"] if r["uid"] == "o11y-disk-low"
    )
    http.overrides["/alert-rules/"] = (500, packed(definition))
    assert queries.evaluate(run, "o11y-disk-low") is None
    assert not any(url.endswith("/api/v1/eval") for _, url, _, _, _ in http.calls)
    http.overrides = {
        "/api/v1/eval": (
            500,
            packed({"results": {"C": {"status": 200, "frames": [frame({}, [0])]}}}),
        )
    }
    assert queries.evaluate(run, "o11y-disk-low") is None
    http.overrides = {
        "/api/dashboards/uid/o11y-health": (500, packed({"dashboard": HEALTH}))
    }
    assert monitoring.panel(run, "Up: o11y/tempo") is None
    http.overrides = {
        "/api/dashboards/uid/o11y-agents-capacity": (
            500,
            packed({"dashboard": DASHBOARD}),
        )
    }
    assert cap.panel(run, "Live windows", 1000) is None
    http.overrides = {"/api/ds/query": (500, table_frames(windows()))}
    assert cap.panel(run, "Live windows", 1000) is None


def test_fires_passes_an_explicit_threshold() -> None:
    _, _, _, run = instance_c()
    assert queries.fires(run, "o11y-rustfs-memory", "job", ["o11y/rustfs"], 0)
