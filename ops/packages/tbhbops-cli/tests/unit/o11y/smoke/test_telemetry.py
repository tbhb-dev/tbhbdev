"""Delta and cumulative smoke telemetry."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import pytest
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    runtime,
    safety,
    telemetry,
)
from unit.o11y.smoke.metrics_fixture import vector
from unit.o11y.smoke.stack_fixture import instance_c
from unit.o11y.smoke.stage_contracts import (
    check_http_failure,
    check_process_failure,
    check_stage_protocol,
    fault_cases,
)

STAGES = ("group11",)


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

        def discarded(_run: runtime.Smoke) -> int:
            return next(observations)

        monkeypatch.setattr(telemetry, "discarded", discarded)
    if mode in {"otlp", "point"}:
        original = telemetry.post

        def post(run: runtime.Smoke, signal: str, body: bytes) -> bool:
            return (
                False
                if engine.delta == "2" and ((body == b"{}") == (mode == "otlp"))
                else original(run, signal, body)
            )

        monkeypatch.setattr(telemetry, "post", post)
    if mode in {"values", "restarted-value"}:
        original_query = http.instant

        def instant(expression: str) -> bytes:
            if expression.startswith("o11y_smoke_delta_") and (
                mode == "values" or engine.delta == "2"
            ):
                return vector([0])
            return original_query(expression)

        monkeypatch.setattr(http, "instant", instant)
    with pytest.raises(safety.SmokeError, match="^11: " + rule + "$"):
        telemetry.group11(run)


@pytest.mark.parametrize("trailer", ["200X", "200 ", "500", "200\n500"])
def test_discard_scrape_keeps_exact_status(trailer: str) -> None:
    _, engine, _, run = instance_c()
    engine.overrides["http://mimir:8080/metrics"] = Result(
        0, "# TYPE cortex_build_info gauge\n\n" + trailer
    )
    with pytest.raises(safety.SmokeError, match=r"^11: discard-scrape$"):
        telemetry.discarded(run)


def test_discard_exposition_and_trailing_newline() -> None:
    _, engine, _, run = instance_c()
    engine.overrides["http://mimir:8080/metrics"] = Result(
        0, "# TYPE cortex_build_info gauge\n\n200\n"
    )
    assert telemetry.discarded(run) == 0
    engine.overrides["http://mimir:8080/metrics"] = Result(0, "not an exposition\n200")
    with pytest.raises(safety.SmokeError, match=r"^11: discard-exposition$"):
        telemetry.discarded(run)


@pytest.mark.parametrize("stage", STAGES)
def test_stage_protocol_contract(stage: str) -> None:
    check_stage_protocol(stage)


@pytest.mark.parametrize(("stage", "match"), fault_cases("commands", STAGES))
def test_every_process_failure_names_a_rule(stage: str, match: str) -> None:
    check_process_failure(stage, match)


@pytest.mark.parametrize(("stage", "match"), fault_cases("http", STAGES))
def test_every_http_failure_names_a_rule(stage: str, match: str) -> None:
    check_http_failure(stage, match)
