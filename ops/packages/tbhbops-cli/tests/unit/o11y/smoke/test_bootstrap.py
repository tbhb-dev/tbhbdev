"""Smoke stack startup and object storage bootstrap."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

lazy from collections.abc import Callable

import pytest
from tbhbops.o11y import init
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    bootstrap,
    runtime,
    safety,
)
from tbhbops_cli.o11y.smoke import setup as setup_stage
from unit.o11y.smoke.runtime_fixture import HOST, OUT, make_system, prepared, run


def test_bucket_replacement_and_dependency_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for number, expected in (
        (2, "2: a second run recreated a bucket"),
        (3, "2: existing bucket recreated"),
    ):
        _, _, instance = prepared()
        setup_stage.group1(instance)
        original = bootstrap.inventory
        calls = 0

        def inventory(
            run: runtime.Smoke,
            original: Callable[[runtime.Smoke], tuple[str, ...]] = original,
            number: int = number,
        ) -> tuple[str, ...]:
            nonlocal calls
            calls += 1
            value = original(run)
            return (value[0] + " changed", *value[1:]) if calls == number else value

        with monkeypatch.context() as patch:
            patch.setattr(bootstrap, "inventory", inventory)
            with pytest.raises(safety.SmokeError) as failure:
                bootstrap.group2(instance)
        assert str(failure.value) == expected
    _, _, instance = prepared()
    setup_stage.group1(instance)
    instance.model["loki"]["depends_on"]["s3-buckets"]["condition"] = "service_started"
    with pytest.raises(safety.SmokeError) as failure:
        bootstrap.group2(instance)
    assert str(failure.value) == "2: backend bucket dependency"


@pytest.mark.parametrize("bad", [init.Refusal("wrong refusal"), init.Plan(())])
def test_group2_requires_init_own_refusal(
    bad: init.Plan | init.Refusal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, instance = prepared()
    setup_stage.group1(instance)
    original = setup_stage.initialize

    def initialize(run: runtime.Smoke) -> init.Plan | init.Refusal:
        value = original(run)
        return bad if isinstance(value, init.Refusal) else value

    monkeypatch.setattr(setup_stage, "initialize", initialize)
    with pytest.raises(safety.SmokeError) as failure:
        bootstrap.group2(instance)
    assert str(failure.value) == "2: init did not refuse damaged key in use"


def test_group2_restored_values_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, instance = prepared()
    setup_stage.group1(instance)
    original = setup_stage.values
    calls = 0

    def values(run: runtime.Smoke) -> dict[str, bytes]:
        nonlocal calls
        calls += 1
        value = original(run)
        if calls == 2:
            value[init.SECRETS[0].name] = b"wrong"
        return value

    monkeypatch.setattr(setup_stage, "values", values)
    with pytest.raises(safety.SmokeError) as failure:
        bootstrap.group2(instance)
    assert str(failure.value) == "2: sentinel key could not be restored"


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("match", "rule"),
    [
        ("cp rustfs:pinned -a /src/.", "2: config copy failed"),
        ("chmod rustfs:pinned -R", "2: config permissions failed"),
        ("up -d alert-stub", "2: alert stubs failed"),
        ("ps -a -q s3-buckets", "2: s3-buckets did not exit 0"),
        ("logs --no-log-prefix s3-buckets", "2: bucket bootstrap failed"),
        ("run --rm -T s3-buckets", "2: bucket bootstrap failed"),
        ("s3probe buckets", "2: bucket inventory unavailable"),
        ("s3probe delete-bucket", "2: partial bootstrap fixture failed"),
        ("logs --no-log-prefix rustfs", "2: rustfs start log unavailable"),
        ("s3probe default-credentials", "2: default login was not refused"),
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
        ("s3probe buckets", Result(0, "bad"), "2: bucket inventory differs"),
        (
            "logs --no-log-prefix s3-buckets",
            Result(0, "bad"),
            "2: bucket bootstrap output",
        ),
        (
            "logs --no-log-prefix rustfs",
            Result(0, "rustfsadmin"),
            "2: rustfs default login",
        ),
        (
            "{{.State.Status}} {{.State.ExitCode}}",
            Result(0, "running 0"),
            "2: s3-buckets did not exit 0",
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
