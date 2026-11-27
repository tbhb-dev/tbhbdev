"""Pure harness launch formatting and environment decisions."""

# The shared conftest creates files for host integration tests.
# gazelle:include_pytest_conftest false

from __future__ import annotations

import tomllib

import pytest

from tbhbdev.alpha.agent import run_host_launch
from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    ClaudePolicy,
    CodexMode,
    CodexPolicy,
    Harness,
    Policy,
    Profile,
    WebSearch,
)

_LAUNCH_ENV = {
    "PATH": "/launch",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "a.b",
    "EXAMPLE": "keep",
}
_CODEX_HOOK_ENV = {"PATH": "/tools", "GIT_CONFIG_COUNT": "0", "EXAMPLE": "keep"}


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("suffix", ["", "/"])
def test_otel_config_derives_signal_paths_from_the_base(
    scheme: str, suffix: str
) -> None:
    base = f"{scheme}://otlp.example.net"
    table = tomllib.loads(run_host_launch.otel_config(base + suffix))["otel"]
    assert table["trace_exporter"]["otlp-http"]["endpoint"] == (f"{base}/v1/traces")
    assert table["metrics_exporter"]["otlp-http"]["endpoint"] == (f"{base}/v1/metrics")


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
    assert run_host_launch.argv_log_line(command) == expected


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
    assert run_host_launch.argv_log_line(command) == (
        "argv: codex -c 'model_reasoning_effort='\"'\"'high'\"'\"'' -m -c"
    )


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_resource_attributes_name_host_account_and_run(scheme: str) -> None:
    assert run_host_launch.resource_attributes(
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
        run_host_launch.resource_attributes(
            None, host="example-linux", account="agent", run_name="example"
        )
        == {}
    )


@pytest.mark.parametrize(
    ("harness", "detail", "unsandboxed", "expected"),
    [
        (
            Harness.CODEX,
            CodexPolicy(CodexMode.PROFILE, (), (), (), "never", WebSearch.DISABLED),
            False,
            _CODEX_HOOK_ENV,
        ),
        (
            Harness.CODEX,
            CodexPolicy(CodexMode.FULL_ACCESS, (), (), (), "never", WebSearch.DISABLED),
            True,
            _CODEX_HOOK_ENV,
        ),
        (
            Harness.CLAUDE,
            ClaudePolicy({}),
            False,
            _LAUNCH_ENV | {"PATH": "/tools"},
        ),
        (Harness.CLAUDE, ClaudePolicy({}), True, _LAUNCH_ENV),
        (Harness.AGY, AgyPolicy(()), False, _LAUNCH_ENV),
        (Harness.AGY, AgyPolicy(()), True, _LAUNCH_ENV),
    ],
)
def test_hook_environment(
    harness: Harness,
    detail: CodexPolicy | ClaudePolicy | AgyPolicy,
    unsandboxed: bool,
    expected: dict[str, str],
) -> None:
    value = Policy(harness, Profile.BUILD, _LAUNCH_ENV.copy(), (), detail, "/tools")
    assert run_host_launch.hook_environment(value, unsandboxed=unsandboxed) == expected
