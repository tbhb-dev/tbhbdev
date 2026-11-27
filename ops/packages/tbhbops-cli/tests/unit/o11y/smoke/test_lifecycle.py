"""Release recreation, restart, and notification transitions."""

from dataclasses import replace
from functools import partial
from pathlib import Path
lazy from collections.abc import Callable, Mapping, Sequence

import pytest
from tbhbops.o11y import smoke
from tbhbops_cli.o11y import receipt
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    lifecycle,
    runtime,
    safety,
)
from test_o11y_cli import Fake
from unit.o11y.smoke.metrics_fixture import HttpC
from unit.o11y.smoke.runtime_fixture import OUT
from unit.o11y.smoke.stack_fixture import SOURCE, EngineC, instance_c
from unit.o11y.smoke.stage_contracts import (
    check_http_failure,
    check_process_failure,
    check_stage_protocol,
    fault_cases,
)

STAGES = ("group8", "group9", "group10", "group13")


def test_release_recreation_and_direct_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, _http, run = instance_c()
    lifecycle.group8(run, SOURCE)
    assert run.release == OUT / "release"
    assert fs.read_bytes(OUT / "src/config/loki/config.yaml").endswith(b"\n#\n")

    def fake_receipt(*_args: object, **kwargs: object) -> tuple[list[str], bool]:
        assert kwargs == {"signals": "traces,logs,metrics", "safe_failures": True}
        return ["ok fixture receipt"], True

    monkeypatch.setattr(receipt, "receipt", fake_receipt)
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
    original = safety.scan_path
    needle = run.sentinels.scan_values()[1]

    def scan(filesystem: Fake, path: Path, needles: tuple[bytes, ...]) -> None:
        target = OUT / place
        if path == target or path in target.parents:
            fs.write_atomic(target, b"prefix\xff" + needle, 0o600)
        original(filesystem, path, needles)

    monkeypatch.setattr(lifecycle, "scan_path", scan)
    with pytest.raises(safety.SmokeError, match=r"sentinel secret") as failure:
        lifecycle.group10(run)
    assert needle.decode() not in str(failure.value)
    assert needle.decode() not in "".join(run.transcript)


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

        def same(_run: runtime.Smoke) -> str:
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
    with pytest.raises(safety.SmokeError, match="^8: " + rule + "$"):
        lifecycle.group8(run, SOURCE)


def test_email_receiver_persists_is_failure() -> None:
    _, _, _, run = instance_c()
    with pytest.raises(safety.SmokeError, match=r"^13: email-receiver-deleted$"):
        lifecycle.group13(run, SOURCE)


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
    with pytest.raises(safety.SmokeError, match="^" + rule + "$"):
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
    with pytest.raises(safety.SmokeError, match="^" + rule + "$"):
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
        safety.SmokeError, match="^10: repeated-owner-running: " + reason + "$"
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
    with pytest.raises(safety.SmokeError, match=r"^10: repeated-owner-output$"):
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
        safety.SmokeError, match=r"^10: repeated-owner-running: identity-mismatch$"
    ):
        lifecycle.group10(run)


def test_repeated_owner_running_failure_survives_stop_failure() -> None:
    _, engine, _, run = instance_c()
    lifecycle.group8(run, SOURCE)
    engine.overrides["docker ps -q --filter"] = Result(0, "")
    engine.overrides["docker stop"] = Result(1, "private")
    with pytest.raises(
        safety.SmokeError, match=r"^10: repeated-owner-running: zero-containers$"
    ):
        lifecycle.group10(run)


@pytest.mark.parametrize("stage", STAGES)
def test_stage_protocol_contract(stage: str) -> None:
    check_stage_protocol(stage)


@pytest.mark.parametrize(("stage", "match"), fault_cases("commands", STAGES))
def test_every_process_failure_names_a_rule(stage: str, match: str) -> None:
    check_process_failure(stage, match)


@pytest.mark.parametrize(("stage", "match"), fault_cases("http", STAGES))
def test_every_http_failure_names_a_rule(stage: str, match: str) -> None:
    check_http_failure(stage, match)
