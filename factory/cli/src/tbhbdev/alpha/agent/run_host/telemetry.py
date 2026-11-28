"""Format run-host telemetry configuration, attributes, and command logs."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_ENV_OVERRIDE = "shell_environment_policy.set."
_NOT_REPORTED = "not reported"


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


def resource_attributes(
    endpoint: str | None, *, host: str, account: str, run_name: str
) -> dict[str, str]:
    """OpenTelemetry resource attributes that name the host and the account.

    A run without a configured endpoint gets none, so it stays as it was.
    """
    if endpoint is None:
        return {}
    return {
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"host.name={host},tbhb.account={account},tbhb.run.id={run_name}"
        )
    }


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


def run_end_attributes(
    usage: Mapping[str, object],
    phase_seconds: Mapping[str, object],
    published: Mapping[str, object],
) -> dict[str, float | str]:
    """The end record's usage, phase seconds and published state as attributes.

    A value that is neither a number nor a string, and a `"not reported"`, is
    left out, so a missing figure never reaches the backend as a zero.
    """
    named = {
        "tbhb.run.usage.input_tokens": usage.get("input_tokens"),
        "tbhb.run.usage.output_tokens": usage.get("output_tokens"),
        "tbhb.run.usage.total_tokens": usage.get("total_tokens"),
        "tbhb.run.usage.cost_usd": usage.get("cost_usd"),
        "tbhb.run.phase.setup_seconds": phase_seconds.get("setup"),
        "tbhb.run.phase.model_seconds": phase_seconds.get("model"),
        "tbhb.run.phase.publication_seconds": phase_seconds.get("publication"),
        "tbhb.run.published.branch": published.get("branch"),
        "tbhb.run.published.pr": published.get("pr"),
    }
    return {
        key: value
        for key, value in named.items()
        if isinstance(value, (int, float, str))
        and not isinstance(value, bool)
        and value != _NOT_REPORTED
    }
