"""Format harness launch configuration, environment and log lines."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Harness

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Policy

_ENV_OVERRIDE = "shell_environment_policy.set."


def otel_config(endpoint: str) -> str:
    """A Codex config with an `[otel]` table that exports traces and metrics.

    ENDPOINT is a base URL, and each signal gets its own OTLP/HTTP path. The
    table leaves out the log exporter on purpose: Codex logs tool arguments and
    an output preview, so `log_user_prompt = false`, which guards only the
    prompt text, wouldn't keep a credential or a file out of the export.
    The table has no headers, so it holds no secret.
    """
    base = endpoint.removesuffix("/")

    def exporter(signal_path: str) -> str:
        url = json.dumps(f"{base}/v1/{signal_path}")
        return f'{{ otlp-http = {{ endpoint = {url}, protocol = "binary" }} }}'

    return (
        "[otel]\n"
        f"trace_exporter = {exporter('traces')}\n"
        f"metrics_exporter = {exporter('metrics')}\n"
        "log_user_prompt = false\n"
    )


def hook_environment(value: Policy, *, unsandboxed: bool) -> dict[str, str]:
    """Reflect the shell overrides each harness applies to its launch environment."""
    if value.harness is Harness.CODEX:
        shell = {
            key: val for key, val in value.env.items() if not key.startswith("GIT_")
        }
        return shell | {"PATH": value.tool_path, "GIT_CONFIG_COUNT": "0"}
    if value.harness is Harness.CLAUDE and not unsandboxed:
        return value.env | {"PATH": value.tool_path}
    return value.env


def argv_log_line(command: tuple[str, ...]) -> str:
    """COMMAND as one log line, without the environment values it sets.

    A Codex run's argv carries the permissions table, which is what a sandbox
    failure needs, and the brief by path or on stdin. The `-c` overrides that set
    the run's environment hold credential-adjacent values, so they stay out.
    """
    shown: list[str] = []
    previous = ""
    for word in command:
        if previous == "-c" and word.startswith(_ENV_OVERRIDE):
            shown.pop()
        else:
            shown.append(word)
        previous = word
    return "argv: " + shlex.join(shown)


def resource_attributes(
    endpoint: str | None, *, host: str, account: str, run_name: str
) -> dict[str, str]:
    """Name the host, account and run when an OpenTelemetry endpoint is set."""
    if endpoint is None:
        return {}
    return {
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"host.name={host},tbhb.account={account},tbhb.run.id={run_name}"
        )
    }
