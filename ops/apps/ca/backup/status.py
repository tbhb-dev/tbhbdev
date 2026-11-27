"""Record the nightly service result and send it to the metrics collector."""

# ruff: noqa: INP001
import argparse
import json
import os
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

FAILED = "ca_backup_last_run_failed"
REFERENCE = "ca_backup_last_success_or_start_timestamp_seconds"


class Status(NamedTuple):
    failed: int
    freshness_reference: int


def read_status(state: Path) -> Status:
    try:
        failed, freshness_reference = json.loads(
            (state / "status.json").read_text(encoding="ascii")
        )
    except FileNotFoundError:
        return Status(0, int((state / "monitor-start").stat().st_mtime))
    return Status(failed, freshness_reference)


def record(state: Path, result: str) -> None:
    reference = (
        int(time.time())
        if result == "success"
        else read_status(state).freshness_reference
    )
    pending = state / "status.pending"
    pending.write_text(json.dumps(Status(int(result != "success"), reference)))
    pending.replace(state / "status.json")


def publish(state: Path, endpoint: str) -> None:
    if urlsplit(endpoint).scheme not in {"http", "https"}:
        message = "The metrics endpoint must use HTTP or HTTPS"
        raise ValueError(message)
    status = read_status(state)
    timestamp = str(time.time_ns())
    metrics = [
        {
            "name": name,
            "gauge": {
                "dataPoints": [
                    {
                        "timeUnixNano": timestamp,
                        "asInt": str(value),
                        "attributes": [
                            {"key": "host", "value": {"stringValue": "nas-a"}}
                        ],
                    }
                ]
            },
        }
        for name, value in zip((FAILED, REFERENCE), status, strict=True)
    ]
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "ca-backup"}}
                    ]
                },
                "scopeMetrics": [{"metrics": metrics}],
            }
        ]
    }
    request = Request(  # noqa: S310  # nosec B310 - HTTP(S) only
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        rejected = (
            json.load(response).get("partialSuccess", {}).get("rejectedDataPoints", 0)
        )
    if int(rejected):
        message = "The metrics collector rejected CA backup status"
        raise RuntimeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_subparsers(dest="mode", required=True)
    mode.add_parser("record").add_argument("--state", type=Path, required=True)
    reporter = mode.add_parser("publish")
    reporter.add_argument("--state", type=Path, required=True)
    reporter.add_argument("--endpoint", required=True)
    args = parser.parse_args()
    if args.mode == "record":
        record(args.state, os.environ["SERVICE_RESULT"])
    else:
        publish(args.state, args.endpoint)


if __name__ == "__main__":
    main()
