"""Synthetic export counter histories and bounded response classifications."""

import json
import re

from tbhbops.o11y.receipt import decode
lazy from tbhbops.o11y.validate import Document

COUNTERS = (
    "otelcol_exporter_send_failed_spans_total",
    "otelcol_exporter_send_failed_metric_points_total",
    "otelcol_exporter_send_failed_log_records_total",
    "otelcol_exporter_enqueue_failed_spans_total",
    "otelcol_exporter_enqueue_failed_metric_points_total",
    "otelcol_exporter_enqueue_failed_log_records_total",
    "prometheus_remote_storage_samples_failed_total",
)
CASES = ("burst", "boundary", "old-burst", "reset", "healed", "new-counter", "ongoing")
STEP = 30
RESET_AT = 1350
NEW_COUNTER_AT = 1560


def counter(case: str, tick: int) -> int:
    if case == "ongoing":
        return tick
    if case == "healed":
        return min(tick, 1200)
    if case == "reset":
        return 10 if tick < RESET_AT else 0
    if case == "new-counter":
        return 10
    boundary = {"burst": 1740, "boundary": 1500, "old-burst": 1200}[case]
    return 10 if tick >= boundary else 0


def histories(end: int) -> bytes:
    metrics = []
    for index, metric in enumerate(COUNTERS):
        for case in CASES:
            points = [
                {
                    "startTimeUnixNano": str((end - 1830) * 1000000000),
                    "timeUnixNano": str((end - 1800 + tick) * 1000000000),
                    "asInt": str(counter(case, tick)),
                    "attributes": [
                        {
                            "key": "component_id",
                            "value": {"stringValue": f"smoke-{index}-{case}"},
                        }
                    ],
                }
                for tick in range(600, 1800 + STEP, STEP)
                if case != "new-counter" or tick >= NEW_COUNTER_AT
            ]
            metrics.append(
                {
                    "name": metric,
                    "sum": {
                        "aggregationTemporality": 2,
                        "isMonotonic": True,
                        "dataPoints": points,
                    },
                }
            )
    return json.dumps(
        {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "o11y/alloy"},
                            }
                        ]
                    },
                    "scopeMetrics": [
                        {"scope": {"name": "o11y-smoke-export"}, "metrics": metrics}
                    ],
                }
            ]
        }
    ).encode()


def production_rule(body: bytes) -> tuple[str, int] | None:
    try:
        data = decode(body)
        match = re.fullmatch(r"([0-9]+)([ms])", data["for"])
        expressions = [
            query["model"]["expr"] for query in data["data"] if query["refId"] == "A"
        ]
        if (
            match is None
            or len(expressions) != 1
            or not isinstance(expressions[0], str)
            or not expressions[0]
        ):
            return None
        pending = int(match[1]) * (60 if match[2] == "m" else 1)
        return expressions[0], pending
    except KeyError, TypeError, ValueError:
        return None


def zero_rejected(value: object) -> bool:
    return value is None or value == "0" or (type(value) in {int, float} and value == 0)


def upload(body: bytes) -> bool:
    try:
        data: Document = json.loads(body)
        if not isinstance(data, dict):
            return False
        return all(
            zero_rejected(data.get(parent, {}).get(child))
            for parent, child in (
                ("partialSuccess", "rejectedDataPoints"),
                ("partial_success", "rejected_data_points"),
            )
        )
    except ValueError, AttributeError, UnicodeError, RecursionError:
        return False


def arrived(body: bytes) -> bool:
    try:
        value: object = decode(body)["data"]["result"][0]["value"][1]
    except KeyError, TypeError, IndexError:
        return False
    else:
        return value == "49"


def sustained(body: bytes, pending: int) -> bool:
    try:
        data = decode(body)
        series = [
            s
            for s in data["data"]["result"]
            if s["metric"]["component_id"].startswith("smoke-")
        ]
        return (
            data["status"] == "success"
            and sorted(s["metric"]["component_id"] for s in series)
            == [f"smoke-{i}-ongoing" for i in range(7)]
            and all(
                len(s["values"]) == pending / STEP + 1
                and all(float(point[1]) > 0 for point in s["values"])
                for s in series
            )
        )
    except KeyError, TypeError, ValueError, IndexError, AttributeError:
        return False


def objects(text: str) -> bool:
    text = text.strip()
    return re.fullmatch(r"[0-9]+", text) is not None and int(text) >= 1
