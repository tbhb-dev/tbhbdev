"""Test telemetry formatting with explicit run metadata and argv values."""

# gazelle:include_pytest_conftest false

from __future__ import annotations

import tomllib

import pytest

from tbhbdev.alpha.agent.run_host import telemetry


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("suffix", ["", "/"])
def test_otel_config_derives_signal_paths_from_the_base(
    scheme: str, suffix: str
) -> None:
    base = f"{scheme}://otlp.example.net"
    table = tomllib.loads(telemetry.otel_config(base + suffix))["otel"]
    assert table["trace_exporter"]["otlp-http"]["endpoint"] == (f"{base}/v1/traces")
    assert table["metrics_exporter"]["otlp-http"]["endpoint"] == (f"{base}/v1/metrics")


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_resource_attributes_name_host_account_and_run(scheme: str) -> None:
    assert telemetry.resource_attributes(
        f"{scheme}://otlp.example.net",
        host="example-linux",
        account="agent",
        run_name="example",
    ) == {
        "OTEL_RESOURCE_ATTRIBUTES": (
            "host.name=example-linux,tbhb.account=agent,tbhb.run.id=example"
        )
    }


def test_resource_attributes_need_an_endpoint() -> None:
    assert (
        telemetry.resource_attributes(
            None, host="example-linux", account="agent", run_name="example"
        )
        == {}
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ((), "argv: "),
        (
            ("shell_environment_policy.set.EXAMPLE=one",),
            "argv: shell_environment_policy.set.EXAMPLE=one",
        ),
        (("-c", "shell_environment_policy.set.EXAMPLE=one"), "argv: "),
        (
            ("codex", "shell_environment_policy.set.EXAMPLE=one"),
            "argv: codex shell_environment_policy.set.EXAMPLE=one",
        ),
        (
            (
                "-c",
                "-c",
                "shell_environment_policy.set.EXAMPLE=one",
                "shell_environment_policy.set.EXAMPLE=two",
            ),
            "argv: -c shell_environment_policy.set.EXAMPLE=two",
        ),
    ],
)
def test_argv_log_line_drops_only_environment_override_pairs(
    command: tuple[str, ...], expected: str
) -> None:
    assert telemetry.argv_log_line(command) == expected


def test_argv_log_line_keeps_other_overrides() -> None:
    command = (
        "codex",
        "-c",
        "shell_environment_policy.set.HOME='x y'",
        "-c",
        "model_reasoning_effort='high'",
        "-c",
        "shell_environment_policy.set.PATH='p'",
        "-m",
        "-c",
    )
    assert telemetry.argv_log_line(command) == (
        "argv: codex -c 'model_reasoning_effort='\"'\"'high'\"'\"'' -m -c"
    )


def test_run_end_attributes_keep_numbers_and_strings_only() -> None:
    usage = {
        "input_tokens": "not reported",
        "output_tokens": "not reported",
        "total_tokens": 54534,
        "cost_usd": 0.5,
        "source": "codex log",
    }
    phases = {"setup": 1.5, "model": 20.0, "publication": None}
    published = {"branch": "agent/example", "pr": None}
    assert telemetry.run_end_attributes(usage, phases, published) == {
        "tbhb.run.usage.total_tokens": 54534,
        "tbhb.run.usage.cost_usd": 0.5,
        "tbhb.run.phase.setup_seconds": 1.5,
        "tbhb.run.phase.model_seconds": 20.0,
        "tbhb.run.published.branch": "agent/example",
    }


def test_run_end_attributes_of_an_unreached_run_are_empty() -> None:
    nothing = {"input_tokens": "not reported", "output_tokens": "not reported"}
    phases = {"setup": None, "model": None, "publication": None}
    assert telemetry.run_end_attributes(nothing, phases, {"branch": None}) == {}


def test_run_end_attributes_pass_input_and_output_tokens() -> None:
    usage = {"input_tokens": 3, "output_tokens": 4}
    assert telemetry.run_end_attributes(usage, {}, {"pr": 12}) == {
        "tbhb.run.usage.input_tokens": 3,
        "tbhb.run.usage.output_tokens": 4,
        "tbhb.run.published.pr": 12,
    }
