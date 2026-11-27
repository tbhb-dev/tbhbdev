"""Service readiness checks and Compose fixtures."""

import json
from dataclasses import replace
lazy from collections.abc import Callable

import pytest
from tbhbops.o11y import smoke
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    readiness,
    safety,
)
from unit.o11y.smoke.runtime_fixture import HOST, OUT, make_system, prepared, run


@pytest.mark.parametrize("what", ["ready", "healthy"])
@pytest.mark.parametrize("code", [0, 17])
def test_readiness_and_health_poll_failure(what: str, code: int) -> None:
    _, engine, instance = prepared()
    engine.fail_match = "exec" if what == "ready" else "{{.State.Health.Status}}"
    engine.replacement = Result(code, "private")
    with pytest.raises(
        safety.SmokeError,
        match="did not become ready" if what == "ready" else "is not healthy",
    ):
        readiness.group3(instance)
    assert instance.system.monotonic() == (180 if what == "ready" else 120)


@pytest.mark.parametrize(
    ("response", "passes"), [("200\n", True), ("200 ", False), ("200X", False)]
)
def test_ready_exact_status(response: str, passes: bool) -> None:
    _, engine, instance = prepared()
    engine.fail_match = "exec"
    engine.replacement = Result(0, response)
    if passes:
        readiness.wait_ready(instance, "loki", 3)
    else:
        with pytest.raises(safety.SmokeError, match="3: loki did not become ready"):
            readiness.wait_ready(instance, "loki", 3)


@pytest.mark.parametrize("service", smoke.SERVICES)
def test_group3_readiness_keeps_group_label(service: str) -> None:
    _, engine, instance = prepared()
    engine.fail_match = smoke.READY[service]
    engine.replacement = Result(0, "503")
    with pytest.raises(
        safety.SmokeError, match=rf"^3: {service} did not become ready$"
    ):
        readiness.group3(instance)


def loki_responder(
    mode: str, calls: list[tuple[str, bool, tuple[str, ...]]]
) -> Callable[..., Result]:
    def inside(url: str, *, body: bool = False, args: tuple[str, ...] = ()) -> Result:
        if url.endswith("/ready"):
            return Result(0, "200")
        calls.append((url, body, args))
        if mode == "204" or (mode == "warmup" and len(calls) > 1):
            return Result(0, "\n204")
        if mode == "transport":
            return Result(7, "private", "private error")
        if mode == "malformed":
            return Result(0, "private")
        return Result(
            0,
            "Ingester is shutting down /private with secret\n"
            + ("503" if mode == "timeout" else "500"),
        )

    return inside


@pytest.mark.parametrize(
    "mode",
    [
        "204",
        "warmup",
        "stopping",
        "active-throttled",
        "transport",
        "timeout",
        "malformed",
    ],
)
def test_loki_write_probe(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, engine, instance = prepared()
    instance.system = replace(instance.system, epoch=lambda: 1000.25)
    original = instance.inside
    calls: list[tuple[str, bool, tuple[str, ...]]] = []

    inside = loki_responder(mode, calls)

    monkeypatch.setattr(instance, "inside", inside)
    if mode in ("204", "warmup"):
        readiness.loki_write_probe(instance)
    else:
        if mode == "active-throttled":
            readiness.wait_ready(instance, "loki", 3)
        expected = (
            "transport-error"
            if mode == "transport"
            else "malformed-status"
            if mode == "malformed"
            else "write-refused"
        )
        with pytest.raises(safety.SmokeError) as failure:
            readiness.loki_write_probe(instance)
        assert str(failure.value) == "5: Loki write probe: " + expected
        assert instance.system.monotonic() == 180
    assert len(calls) == (1 if mode == "204" else 2 if mode == "warmup" else 91)
    for url, body, args in calls:
        assert url == "http://loki:3100/loki/api/v1/push"
        assert body is True
        assert args[:5] == ("-X", "POST", "-H", "Content-Type: application/json", "-d")
        assert json.loads(args[-1]) == {
            "streams": [
                {
                    "stream": {"service_name": "o11y-smoke-probe"},
                    "values": [["1000000000000", "o11y-smoke-probe"]],
                }
            ]
        }
    # Exercise the real in-network argument builder with that same payload.
    engine.fail_match = "exec"
    engine.replacement = Result(0, "\n204")
    original(calls[0][0], body=True, args=calls[0][2])
    assert engine.calls[-1][0][-7:] == [
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "-d",
        calls[0][2][-1],
        "http://loki:3100/loki/api/v1/push",
    ]


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("match", "rule"),
    [
        ("up -d --wait --wait-timeout 180", "3a: unmodified up --wait failed"),
        ("rm -sf readiness-unhealthy", "3a: fixture removal failed"),
        ("rm -sf readiness-failed", "3a: fixture removal failed"),
    ],
)
def test_stage_command_failure_rules(match: str, rule: str) -> None:
    fs, engine, system = make_system()
    engine.fail_match = match
    code, output = run(system)
    assert code == 1
    assert "FAIL " + rule + "\n" in output
    assert "untrusted" not in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("match", "result", "rule"),
    [
        (
            "http://loki:3100/config",
            Result(0, "ingester:\n  wal:\n    disk_full_threshold: 0.90\n200"),
            "3: Loki WAL threshold: missing or unexpected value",
        ),
        (
            "http://loki:3100/config",
            Result(17, "private"),
            "3: Loki WAL threshold: configuration unavailable",
        ),
        (
            "ps -a -q readiness-unhealthy",
            Result(0, ""),
            "3a: fixture failed for an unintended cause",
        ),
        (
            "{{.State.Health.Status}} readiness-unhealthy",
            Result(0, "healthy"),
            "3a: fixture failed for an unintended cause",
        ),
        (
            "{{.State.Status}}:{{.State.ExitCode}} readiness-failed",
            Result(0, "exited:1"),
            "3a: fixture failed for an unintended cause",
        ),
        (
            "up -d --wait --wait-timeout 30",
            Result(0, "private"),
            "3a: up --wait accepted fixture or timed out",
        ),
        (
            "up -d --wait --wait-timeout 30",
            Result(None, "private"),
            "3a: up --wait accepted fixture or timed out",
        ),
    ],
)
def test_stage_parsed_failure_rules(match: str, result: Result, rule: str) -> None:
    fs, engine, system = make_system()
    engine.fail_match = match
    engine.replacement = result
    code, output = run(system)
    assert code == 1
    assert "FAIL " + rule + "\n" in output
    assert "private" not in output
    assert fs.lstat(OUT) is None
