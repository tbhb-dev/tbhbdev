"""Expectations and safe classifications for the disposable stack smoke test."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from tbhbops.o11y import init
lazy from tbhbops.o11y.validate import Document

if TYPE_CHECKING:
    from collections.abc import Mapping

# Long-running services in ops' compose.yaml; one-shots are checked separately.
SERVICES = ("rustfs", "loki", "tempo", "mimir", "alloy", "grafana")
BUCKETS = ("loki", "mimir-blocks", "mimir-ruler", "tempo")
READY = dict(
    zip(
        SERVICES,
        (
            "http://127.0.0.1:9000/health/ready",
            "http://loki:3100/ready",
            "http://tempo:3200/ready",
            "http://mimir:8080/ready",
            "http://alloy:12345/-/ready",
            "http://grafana:3000/api/health",
        ),
        strict=True,
    )
)
SUCCESS = {
    "1": (
        "init creates the layout, secrets and alert placeholders, "
        "keeps valid ones, and completes an interrupted or truncated first run"
    ),
    "2": (
        "up succeeds, buckets are created once and completed "
        "after a partial run, rustfs refuses the default login, "
        "init refuses to replace a key in use"
    ),
    "3": "all six services are ready, and all six report healthy",
    "3a": (
        "unmodified up --wait succeeds; unhealthy and exit-42 "
        "fixtures each fail for their intended cause"
    ),
}
PORTS = (13000, 14317, 14318)
FIXED_ENV = {
    "O11Y_GRAFANA_ROOT_URL": "http://127.0.0.1:13000/",
    "O11Y_GRAFANA_OWNER_LOGIN": "owner@example.invalid",
    "O11Y_GRAFANA_PORT": "13000",
    "O11Y_OTLP_GRPC_PORT": "14317",
    "O11Y_OTLP_HTTP_PORT": "14318",
    "O11Y_ENVIRONMENT": "smoke",
    "O11Y_HOST_NAME": "smoke-host",
    "O11Y_DISK_RESERVE_GB": "999999",
    "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD": "0.99",
    "O11Y_ALERT_EMAIL_TO": "smoke-to@example.invalid",
    "O11Y_ALERT_SMTP_HOST": "alert-mail",
    "O11Y_ALERT_SMTP_PORT": "1025",
    "O11Y_ALERT_SMTP_USER": "smoke-user",
    "O11Y_ALERT_SMTP_FROM": "smoke-from@example.invalid",
    "O11Y_ALERT_SMTP_SKIP_VERIFY": "true",
}
THROTTLE = "disk usage exceeded threshold, throttling writes"
RESUME = "disk usage below threshold, resuming writes"
LIMIT = 4 * 1024 * 1024
EMPTY_SECRET = b""


# Fixed FAIL wording shared by stages and their callers.
FAILURES = {
    "interrupted": "interrupted",
    "output_exceeds_4_mib": "output exceeds 4 MiB",
    "data_dir_symlink_or_inspection_failure": "data dir symlink or inspection failure",
    "setup_model_unavailable": "setup: model unavailable",
    "setup_utility_image_unavailable": "setup: utility image unavailable",
    "setup_leftover_project_cleanup_failed": "setup: leftover project cleanup failed",
    "1_init_failed": "1: init failed",
    "1_s3_env_does_not_match_the_key_files": "1: s3.env does not match the key files",
    "1_alert_placeholders_differ": "1: alert placeholders differ",
    "1_init_replaced_a_valid_existing_secret": (
        "1: init replaced a valid existing secret"  # nosec B105: diagnostic text
    ),
    "1_a_second_init_changed_a_file": "1: a second init changed a file",
    "1_interrupted_init_recovery_changed_a_file": (
        "1: interrupted init recovery changed a file"
    ),
    "1_truncated_key_was_left_in_place": "1: truncated key was left in place",
    "1_sentinel_key_could_not_be_restored": "1: sentinel key could not be restored",
    "2_bucket_inventory_unavailable": "2: bucket inventory unavailable",
    "2_bucket_inventory_differs": "2: bucket inventory differs",
    "2_bucket_bootstrap_failed": "2: bucket bootstrap failed",
    "2_config_copy_failed": "2: config copy failed",
    "2_config_permissions_failed": "2: config permissions failed",
    "2_up_failed": "2: up failed",
    "2_alert_stubs_failed": "2: alert stubs failed",
    "2_s3_buckets_did_not_exit_0": "2: s3-buckets did not exit 0",
    "2_a_second_run_recreated_a_bucket": "2: a second run recreated a bucket",
    "2_partial_bootstrap_fixture_failed": "2: partial bootstrap fixture failed",
    "2_rustfs_start_log_unavailable": "2: rustfs start log unavailable",
    "2_default_login_was_not_refused": "2: default login was not refused",
    "2_init_did_not_refuse_damaged_key_in_use": (
        "2: init did not refuse damaged key in use"
    ),
    "2_refused_init_changed_a_file": "2: refused init changed a file",
    "2_sentinel_key_could_not_be_restored": "2: sentinel key could not be restored",
    "3a_unmodified_up_wait_failed": "3a: unmodified up --wait failed",
    "3a_up_wait_accepted_fixture_or_timed_out": (
        "3a: up --wait accepted fixture or timed out"
    ),
    "3a_fixture_failed_for_an_unintended_cause": (
        "3a: fixture failed for an unintended cause"
    ),
    "3a_fixture_removal_failed": "3a: fixture removal failed",
    "usage_overlapping_paths": "usage: overlapping paths",
    "setup_local_engine_refused": "setup: local engine refused",
    "usage_symlink_path_refused": "usage: symlink path refused",
    "usage_output_must_be_an_empty_directory": (
        "usage: output must be an empty directory"
    ),
    "unsafe_data_dir": "unsafe data dir",
    "setup_model_does_not_load_without_secrets": (
        "setup: model does not load without secrets"
    ),
    "setup_leftover_data_cleanup_failed": "setup: leftover data cleanup failed",
    "setup_rendered_file_unavailable": "setup: rendered file unavailable",
    "init_data_copy_failed": "init: data copy failed",
    "init_owner_update_failed": "init: owner update failed",
    "init_mode_update_failed": "init: mode update failed",
    "1_fingerprint_unavailable": "1: fingerprint unavailable",
    "1_layout_unavailable": "1: layout unavailable",
    "2_rustfs_guard_copy_failed": "2: RustFS guard copy failed",
    "2_rustfs_guard_ownership_failed": "2: RustFS guard ownership failed",
    "container_state_unavailable": "container state unavailable",
    "diagnostics_capture_failed_no_logs_promoted": (
        "diagnostics: capture failed; no logs promoted"
    ),
    "init_host_ownership_unavailable": "init: host ownership unavailable",
    "usage_loopback_port_already_bound": "usage: loopback port already bound",
}


MESSAGES = {
    "smoke_setup": "smoke setup",
    "smoke_loki_wal_threshold_0_99": "smoke Loki WAL threshold: 0.99",
    "smoke_diagnostics_clean_alloy_and_loki_logs_retained": (
        "smoke diagnostics: clean Alloy and Loki logs retained"
    ),
    "smoke_diagnostics_cleanup_failed": "smoke diagnostics: cleanup failed",
    "smoke_diagnostics_capture_unavailable_stack_not_started": (
        "smoke diagnostics: capture unavailable (stack not started)"
    ),
    "smoke_diagnostics_transcript_finalization_failed": (
        "smoke diagnostics: transcript finalization failed"
    ),
    "fail_interrupted": "FAIL interrupted\n",
    "fail_smoke_operation_unavailable": (
        "FAIL smoke: operation unavailable ({error}; {progress})"
    ),
    "smoke_operation_plan_incomplete": "{failure}: {message}",
    "smoke_operation_errno": " [{name}]",
    "smoke_before_group_1": "before group 1",
    "smoke_operation_progress": "{group}, step {step}",
    "smoke_diagnostics_capture_or_scan_failed_no_logs_promoted": (
        "smoke diagnostics: capture or scan failed; no logs promoted"
    ),
    "smoke_diagnostics_disk_evidence_unavailable": (
        "smoke diagnostics: disk evidence unavailable"
    ),
}


@dataclass(frozen=True)
class Sentinels:
    values: tuple[tuple[str, bytes], ...]
    token: bytes

    def scan_values(self) -> tuple[bytes, ...]:
        return (*(value.rstrip(b"\n") for _, value in self.values), self.token)


def sentinels(draws: tuple[bytes, ...]) -> Sentinels:
    """Map independent random draws into init's accepted alphabets."""
    values = []
    for secret, draw in zip(init.SECRETS, draws[: len(init.SECRETS)], strict=True):
        value = bytes(ord(secret.charset[b % len(secret.charset)]) for b in draw)
        if len(value) != secret.length:
            message = "invalid random draw length"
            raise ValueError(message)
        values.append((secret.name, value + b"\n"))
    token = draws[-2].hex().encode()
    values.extend(
        (
            (init.WEBHOOK, b"http://alert-stub:8080/cgi-bin/hook/12345/" + token),
            (init.SMTP, draws[-1].hex().encode()),
        )
    )
    return Sentinels(tuple(values), token)


def scan(content: bytes, needles: tuple[bytes, ...]) -> str | None:
    """Scan raw bytes, including binary logs; an empty search cannot pass."""
    if not content:
        return "scan empty"
    if any(needle in content for needle in needles):
        return "sentinel secret"
    return None


def sanitize_loki_error(text: str, needles: tuple[bytes, ...]) -> str:
    """Keep a short diagnostic, redacting sentinels before character filtering."""
    for needle in needles:
        # ASCII codec aliases have identical behavior.
        decoded = needle.decode("ascii")  # pragma: no mutate
        text = text.replace(decoded, "[redacted]")
    text = re.sub(r"[^A-Za-z0-9 .,;:_=/\n\[\]-]", "", text)
    return (
        " ".join("[path]" if "/" in word else word for word in text.split())[:120]
        or "none"
    )


def disk_ratio(counts: str) -> tuple[str, bool] | None:
    """Use total minus free, comparing pressure before six-place rounding."""
    match = re.fullmatch(r"([0-9]{1,12})[ \t]+([0-9]{1,12})\n?", counts)
    if match is None:
        return None
    total, free = map(int, match.groups())
    if total == 0 or free > total:
        return None
    used = total - free
    scaled = (used * 1000000 + total // 2) // total
    return f"{scaled // 1000000}.{scaled % 1000000:06d}", used * 10 >= total * 9


def disk_record(counts: str, phase: str) -> str:
    ratio = disk_ratio(counts)
    return f"smoke Loki disk usage ({phase}): {ratio[0] if ratio else 'unavailable'}"


def disk_evidence(counts: str, logs: bytes) -> tuple[str, ...]:
    ratio = disk_ratio(counts)
    if ratio is None:
        line = "smoke diagnostics: Loki disk usage unavailable"
    else:
        relation = (
            "at or above 0.90 (pressure at collection time)"
            if ratio[1]
            else "below 0.90"
        )
        line = f"smoke diagnostics: Loki disk usage ratio={ratio[0]} {relation}"
    # Only fixed phrases enter the transcript, never log labels or paths.
    transitions = tuple(
        phrase for phrase in (THROTTLE, RESUME) if phrase.encode() in logs
    )
    conclusion = (
        "confirmed by transition line"
        if THROTTLE in transitions
        else "inconclusive (no throttling transition in head and tail windows)"
        if transitions
        else "inconclusive (no transition in head and tail windows)"
    )
    return (line, *transitions, f"smoke diagnostics: Loki WAL throttling {conclusion}")


def wal_threshold(code: int | None, response: str) -> str | None:
    body, separator, status = response.rstrip("\n").rpartition("\n")
    if code != 0 or not separator or status != "200":
        return "configuration unavailable"
    ingester = wal = (
        False  # pragma: no mutate  # None and False are equivalent initial false states
    )
    values = []
    for line in body.splitlines():
        if line and not line.startswith(" "):
            ingester, wal = line == "ingester:", False
        if ingester and re.match(r"^  [^ ]", line):
            wal = line == "  wal:"
        if ingester and wal and line.startswith("    disk_full_threshold:"):
            values.append(line.split())
    return (
        None
        if values == [["disk_full_threshold:", "0.99"]]
        else "missing or unexpected value"
    )


def loki_write_rule(code: int | None, response: str) -> str | None:
    """Readiness is insufficient when an ingester's WAL refuses writes."""
    if code != 0:
        return "transport-error"
    _, separator, status = response.rstrip("\n").rpartition("\n")
    if not separator or re.fullmatch(r"[0-9]{3}", status) is None:
        return "malformed-status"
    return None if status == "204" else "write-refused"


def dependencies(services: Mapping[str, Document]) -> str | None:
    try:
        for service in ("loki", "tempo", "mimir"):
            if (
                services[service]["depends_on"]["s3-buckets"]["condition"]
                != "service_completed_successfully"
            ):
                return "backend bucket dependency"
        if (
            services["s3-buckets"]["depends_on"]["rustfs"]["condition"]
            != "service_healthy"
        ):
            return "bucket health dependency"
    except KeyError, TypeError:
        return "missing bucket dependency"
    return None


def bucket_log(text: str, created: tuple[str, ...]) -> str | None:
    lines = sorted(
        line
        for line in text.replace("\r", "").splitlines()
        if line.startswith("bucket ")
    )
    expected = [
        f"bucket {name} {'created' if name in created else 'exists'}"
        for name in BUCKETS
    ]
    return None if lines == expected else "bucket bootstrap output"


def bucket_inventory(text: str) -> tuple[str, ...] | None:
    lines = tuple(sorted(text.splitlines()))
    names = tuple(line.split()[0] for line in lines if line.split())
    return lines if names == BUCKETS and len(lines) == len(BUCKETS) else None


def kept_buckets(before: tuple[str, ...], after: tuple[str, ...]) -> str | None:
    def kept(lines: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(line for line in lines if line.split()[0] != "mimir-ruler")

    return None if kept(before) == kept(after) else "existing bucket recreated"


def layout(
    values: Mapping[str, bytes], owner: tuple[int, int]
) -> dict[str, tuple[int, int, int, int | None]]:
    expected: dict[str, tuple[int, int, int, int | None]] = {
        d.name: (d.uid, d.gid, d.mode, None) for d in init.DIRECTORIES
    }
    expected.update(
        {
            f"secrets/{s.name}": (s.uid, s.gid, s.mode, len(values[s.name]))
            for s in init.SECRETS
        }
    )
    expected.update(
        {
            f"secrets/{name}": (472, 472, 0o400, len(values[name]))
            for name in (init.WEBHOOK, init.SMTP)
        }
    )
    expected["secrets/s3.env"] = (*owner, 0o600, len(s3_env(values)))
    return expected


def s3_env(values: Mapping[str, bytes]) -> bytes:
    access, secret = (values[s.name].rstrip(b"\n") for s in init.SECRETS[:2])
    return b"AWS_ACCESS_KEY_ID=" + access + b"\nAWS_SECRET_ACCESS_KEY=" + secret + b"\n"


def layout_rule(
    actual: Mapping[str, tuple[int, int, int, int | None]],
    expected: Mapping[str, tuple[int, int, int, int | None]],
) -> str | None:
    return (
        None if actual == expected else "owners, modes or sizes differ from the layout"
    )


def recreated(
    before: Mapping[str, tuple[str, str]], after: Mapping[str, tuple[str, str]]
) -> str | None:
    """The group 8 expectation also gives later slices a parsed-state classifier."""
    if set(before) != set(SERVICES) or set(after) != set(SERVICES):
        return "unexpected container state count"
    for name in SERVICES:
        old_id, old_time = before[name]
        new_id, new_time = after[name]
        if not all((old_id, old_time, new_id, new_time)):
            return "malformed container state"
        failure = _recreated_service(name, before[name], after[name])
        if failure is not None:
            return failure
    return None


def _recreated_service(
    name: str, before: tuple[str, str], after: tuple[str, str]
) -> str | None:
    old_id, old_time = before
    new_id, new_time = after
    if name == "loki":
        if old_id == new_id:
            return "loki was not recreated"
    elif name in ("alloy", "grafana"):
        if old_id != new_id or datetime.fromisoformat(
            new_time
        ) <= datetime.fromisoformat(old_time):
            return "dependent was not restarted in place"
    elif before != after:
        return "unrelated service changed"
    return None
