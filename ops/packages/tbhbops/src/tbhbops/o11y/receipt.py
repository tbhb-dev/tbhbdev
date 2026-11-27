"""Receipt payloads and safe response classifiers."""

import json
import re
from enum import StrEnum
from typing import Never
from urllib.parse import quote, urlsplit

lazy from tbhbops.o11y.validate import Document


class QueryKind(StrEnum):
    TRACE = "trace"
    GRPC = "grpc"
    LOG = "log"
    STORED = "stored"
    FRESH = "fresh"
    TIMESTAMP = "timestamp"


URI_SAFE = ""
FRESHNESS = 120
HTTP_OK = 200
STATE_FIELDS = 4
PAIR_FIELDS = 2


def base_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parts.port is None
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        msg = "expected loopback HTTP URL with explicit port"
        raise ValueError(msg)
    return url.rstrip("/")


def state_line(run: str, trace: str, value: int, sent: int) -> str:
    return f"{run} {trace} {value} {sent}\n"


def parse_state(text: str) -> tuple[str, str, int, int]:
    fields = text.split()
    if len(fields) != STATE_FIELDS or not re.fullmatch(
        r"[0-9a-fA-F]{16} [0-9a-fA-F]{32} [0-9]+ [0-9]+", " ".join(fields)
    ):
        msg = "invalid receipt state"
        raise ValueError(msg)
    return fields[0], fields[1], int(fields[2]), int(fields[3])


def payload(  # OTLP receipt fields
    signal: str, run: str, trace: str, span: str, value: int, sent: int
) -> bytes:
    resource = {
        "attributes": [
            {"key": "service.name", "value": {"stringValue": f"o11y-check-{run}"}}
        ]
    }
    scope = {"name": "o11y-check"}
    stamp = str(sent * 1_000_000_000)
    if signal == "traces":
        data = {
            "resourceSpans": [
                {
                    "resource": resource,
                    "scopeSpans": [
                        {
                            "scope": scope,
                            "spans": [
                                {
                                    "traceId": trace,
                                    "spanId": span,
                                    "name": "o11y-check",
                                    "kind": 1,
                                    "startTimeUnixNano": stamp,
                                    "endTimeUnixNano": str(
                                        sent * 1_000_000_000 + 500_000_000
                                    ),
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    elif signal == "logs":
        data = {
            "resourceLogs": [
                {
                    "resource": resource,
                    "scopeLogs": [
                        {
                            "scope": scope,
                            "logRecords": [
                                {
                                    "timeUnixNano": stamp,
                                    "severityText": "INFO",
                                    "body": {"stringValue": f"o11y-check {run}"},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    else:
        data = {
            "resourceMetrics": [
                {
                    "resource": resource,
                    "scopeMetrics": [
                        {
                            "scope": scope,
                            "metrics": [
                                {
                                    "name": "o11y_check_value",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asInt": str(value),
                                                "timeUnixNano": stamp,
                                                "attributes": [
                                                    {
                                                        "key": "run_id",
                                                        "value": {"stringValue": run},
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    return json.dumps(data).encode()


def _invalid_constant(_value: str) -> Never:
    raise ValueError


def decode(body: bytes) -> Document:
    try:
        value: Document = json.loads(body, parse_constant=_invalid_constant)
    except ValueError, UnicodeError:
        return {}
    return value


def log_classify(  # fixed diagnostic cases
    status: int, body: bytes, run: str, floor: int, transport: str = ""
) -> str:
    if transport:
        return f"case=transport-error transport={transport}"
    if status != HTTP_OK:
        return "case=http-error"
    try:
        data: Document = json.loads(body, parse_constant=_invalid_constant)
    except ValueError, UnicodeError:
        return "case=invalid-json"
    if not isinstance(data, dict):
        return "case=invalid-response-shape"
    if isinstance(data.get("status"), str) and data["status"] != "success":
        return "case=loki-error"
    if (
        data.get("status") != "success"
        or not isinstance(data.get("data"), dict)
        or not isinstance(data["data"].get("result"), list)
    ):
        return "case=invalid-response-shape"
    lines = _log_lines(data["data"]["result"])
    if lines is None:
        return "case=invalid-response-shape"
    return _classify_lines(lines, run, floor)


def _log_lines(streams: list[Document]) -> list[list[str]] | None:
    lines: list[list[str]] = []
    for stream in streams:
        if not isinstance(stream, dict) or not isinstance(stream.get("values"), list):
            return None
        values: list[list[str]] = stream["values"]
        for line in values:
            if (
                not isinstance(line, list)
                or len(line) != PAIR_FIELDS
                or not isinstance(line[0], str)
                or not re.fullmatch(r"[0-9]{1,20}", line[0])
                or not isinstance(line[1], str)
            ):
                return None
            lines.append(line)
    return lines


def _classify_lines(lines: list[list[str]], run: str, floor: int) -> str:
    count = len(lines)
    if count == 0:
        return "case=zero-lines count=0"
    if count > 1:
        return f"case=multiple-lines count={count} cap=10"
    if lines[0][1] != f"o11y-check {run}":
        return "case=body-mismatch count=1"
    if int(lines[0][0]) < floor * 1_000_000_000:
        return f"case=older-than-floor count=1 timestamp_ns={lines[0][0]}"
    return "case=accepted count=1"


def trace_found(body: bytes, service: str) -> bool:
    data = decode(body)
    try:
        names = [
            a["value"].get("stringValue")
            for batch in data["batches"]
            for a in batch.get("resource", {}).get("attributes", [])
            if a.get("key") == "service.name"
        ]
    except AttributeError, KeyError, TypeError:
        return False
    return names == [service]


def _series(body: bytes) -> Document:
    result = decode(body)["data"]["result"]
    if len(result) != 1:
        raise ValueError
    series: Document = result[0]
    return series


def metric_found(body: bytes, value: int, mode: str) -> bool:
    try:
        mode = QueryKind(mode)
        result = _series(body)
        if mode == QueryKind.STORED:
            return any(sample[1] == str(value) for sample in result["values"])
        sample: object = result["value"][1]
        return sample == str(value)
    except KeyError, TypeError, ValueError, IndexError:
        return False


def metric_timestamp(body: bytes, now: int) -> bool:
    # The server's timestamp() and our clock are the same clock on loopback,
    # so the freshness window needs no skew handling.
    try:
        sample = _series(body)["value"][1]
        return float(sample) >= now - FRESHNESS
    except KeyError, TypeError, ValueError, IndexError:
        return False


def grpc_found(body: bytes) -> bool:
    data = decode(body)
    return (
        isinstance(data, dict)
        and isinstance(data.get("traces"), list)
        and len(data["traces"]) >= 1
    )


def query_path(kind: str, run: str, trace: str, sent: int, now: int) -> str:
    kind = QueryKind(kind)
    service = f"o11y-check-{run}"
    selector = quote(f'o11y_check_value{{run_id="{run}"}}', safe=URI_SAFE)
    prefix = "/api/datasources/proxy/uid/"
    if kind == QueryKind.TRACE:
        return f"{prefix}tempo/api/traces/{trace}"
    if kind == QueryKind.GRPC:
        query = quote(
            f'{{resource.service.name="o11y-check-grpc-{run}"}}', safe=URI_SAFE
        )
        return f"{prefix}tempo/api/search?q={query}&limit=5"
    if kind == QueryKind.LOG:
        query = quote(f'{{service_name="{service}"}}', safe=URI_SAFE)
        return (
            f"{prefix}loki/loki/api/v1/query_range?query={query}"
            f"&start={sent - 60}000000000&end={now + 60}000000000&limit=10"
        )
    if kind == QueryKind.STORED:
        return (
            f"{prefix}mimir/api/v1/query_range?query={selector}"
            f"&start={sent - 60}&end={now}&step=15"
        )
    query = f"timestamp({selector})" if kind == QueryKind.TIMESTAMP else selector
    return f"{prefix}mimir/api/v1/query?query={query}"
