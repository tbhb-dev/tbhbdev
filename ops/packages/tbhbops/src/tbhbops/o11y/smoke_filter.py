"""Exact Codex span fixture identities, parents and attributes."""

import base64
import json
import re

from tbhbops.o11y.receipt import decode
lazy from tbhbops.o11y.validate import Document

ORPHAN_INDEX = 9
DROPPED = (
    "receiving",
    "handle_responses",
    "persist_rollout_items",
    "realtime_conversation.running_state",
    "fs.get_metadata",
    "fs.read_file",
)
RETAINED = ("turn/start", "tool/exec", "request/send", "child/retained")


def fixture(service: str, trace: str, sent: int) -> bytes:
    names = [RETAINED[0], *DROPPED]
    if service == "codex_exec":
        names.extend(RETAINED[1:])
    spans = []
    for index, name in enumerate(names):
        span = {
            "traceId": trace,
            "spanId": f"{index + 1:016x}",
            "name": name,
            "kind": 1,
            "startTimeUnixNano": str(sent * 1000000000),
            "endTimeUnixNano": str(sent * 1000000000 + 500000000),
            "attributes": [
                {"key": "fixture.name", "value": {"stringValue": name}},
                {"key": "fixture.index", "value": {"intValue": str(index)}},
                {"key": "fixture.kept", "value": {"boolValue": True}},
            ],
        }
        if index:
            span["parentSpanId"] = f"{3 if index == ORPHAN_INDEX else 1:016x}"
        spans.append(span)
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": service}}
                        ]
                    },
                    "scopeSpans": [
                        {"scope": {"name": "o11y-filter-check"}, "spans": spans}
                    ],
                }
            ]
        }
    ).encode()


def span_id(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{16}", value):
        return value
    return base64.b64decode(value, validate=True).hex()


def normalized(span: Document) -> Document:
    return {
        "spanId": span_id(span["spanId"]),
        "name": span["name"],
        "parentSpanId": span_id(span["parentSpanId"])
        if span.get("parentSpanId")
        else "",
        "attributes": sorted(span["attributes"], key=lambda a: a["key"]),
    }


def found(body: bytes, service: str, payload: bytes) -> bool:
    try:
        actual = decode(body)["batches"]
        services = {
            attribute["value"]["stringValue"]
            for batch in actual
            for attribute in batch["resource"]["attributes"]
            if attribute["key"] == "service.name"
        }
        if services != {service}:
            return False
        expected = [
            normalized(span)
            for batch in decode(payload)["resourceSpans"]
            for scope in batch["scopeSpans"]
            for span in scope["spans"]
            if service not in {"codex_exec", "codex_tui"} or span["name"] in RETAINED
        ]
        received = [
            normalized(span)
            for batch in actual
            for scope in batch["scopeSpans"]
            for span in scope["spans"]
        ]
        return sorted(received, key=lambda s: s["spanId"]) == sorted(
            expected, key=lambda s: s["spanId"]
        )
    except KeyError, TypeError, ValueError, AttributeError:
        return False
