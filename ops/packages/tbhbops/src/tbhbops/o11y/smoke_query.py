"""Query, evaluation, monitoring and alert fixture classifications."""

# pylint: disable=duplicate-code  # independent OTLP fixture contract

import json
import math
import re
lazy from collections.abc import Sequence

from tbhbops.o11y.receipt import decode
lazy from tbhbops.o11y.validate import Document

HTTP_OK = 200


def vector(body: bytes) -> list[Document] | None:
    try:
        data = decode(body)
        rows = data["data"]["result"]
        return rows if data["status"] == "success" and isinstance(rows, list) else None
    except KeyError, TypeError:
        return None


def values(body: bytes) -> list[str] | None:
    try:
        data = decode(body)
        rows: list[Document] = data["data"]["result"]
        return (
            [row["value"][1] for row in rows]
            if data["status"] == "success" and isinstance(rows, list)
            else None
        )
    except KeyError, TypeError, IndexError:
        return None


def scrape_limits(text: str) -> dict[str, int]:
    blocks = re.findall(r'prometheus.scrape "[^"]+" \{(.*?)\n\}', text, re.DOTALL)
    limits = {}
    for block in blocks:
        job = re.search(r'"job" = "([^"]+)"', block)
        limit = re.search(r"\bsample_limit\s*=\s*([0-9]+)", block)
        if job is None or limit is None:
            message = "7: scrape-limit-config"
            raise ValueError(message)
        limits[job[1]] = int(limit[1])
    if not limits:
        message = "7: scrape-limit-config"
        raise ValueError(message)
    return limits


def all_up(body: bytes, jobs: tuple[str, ...]) -> bool:
    try:
        rows = vector(body)
        return rows is not None and sorted(
            r["metric"]["job"]
            for r in rows
            if r["metric"].get("cluster") == "o11y"
            and r["metric"].get("namespace") == "o11y"
        ) == sorted(jobs)
    except KeyError, TypeError:
        return False


def evaluation_request(body: bytes, threshold: int | None) -> tuple[str, bytes] | None:
    try:
        rule = decode(body)
        condition = rule["condition"]
        data: list[Document] = rule["data"]
        if (
            not isinstance(condition, str)
            or not condition
            or not isinstance(data, list)
        ):
            return None
        if threshold is not None:
            node = next(node for node in data if node["refId"] == condition)
            node["model"]["conditions"][0]["evaluator"]["params"][0] = threshold
        return condition, json.dumps({"condition": condition, "data": data}).encode()
    except KeyError, TypeError, IndexError, StopIteration:
        return None


def frames(body: bytes) -> dict[str, list[Document]] | None:
    try:
        results: Document = decode(body)["results"]
        if not isinstance(results, dict) or not results:
            return None
        if any(r["status"] != HTTP_OK or r.get("error") for r in results.values()):
            return None
        return {ref: r["frames"] for ref, r in results.items()}
    except KeyError, TypeError, AttributeError:
        return None


def numbers(body: bytes) -> list[Document] | None:
    try:
        results = frames(body)
        if results is None:
            return None
        return [
            {"labels": field.get("labels", {}), "values": frame["data"]["values"][i]}
            for group in results.values()
            for frame in group
            for i, field in enumerate(frame["schema"]["fields"])
            if field["type"] == "number"
        ]
    except KeyError, TypeError, IndexError, AttributeError:
        return None


def evaluated(body: bytes, condition: str) -> list[Document] | None:
    results = frames(body)
    if results is None or condition not in results:
        return None
    return numbers(
        json.dumps(
            {"results": {condition: {"status": 200, "frames": results[condition]}}}
        ).encode()
    )


def firing(
    rows: list[Document] | None, label: str, expected: Sequence[str | None]
) -> bool:
    try:
        return rows is not None and sorted(
            (r["labels"].get(label) for r in rows if r["values"][0] == 1),
            key=str,
        ) == sorted(expected, key=str)
    except KeyError, TypeError, IndexError, AttributeError:
        return False


def observed_labels(rows: list[Document] | None, label: str) -> str:
    """Describe firing labels without including query payloads."""
    if rows is None:
        return "no usable result"
    try:
        return str(
            sorted(
                (row["labels"].get(label) for row in rows if row["values"][0] == 1),
                key=str,
            )
        )
    except KeyError, TypeError, IndexError, AttributeError:
        return "malformed row"


def quiet(rows: list[Document] | None) -> bool:
    try:
        return rows is not None and all(r["values"][0] != 1 for r in rows)
    except KeyError, TypeError, IndexError:
        return False


def panel_expression(body: bytes, title: str) -> str | None:
    try:
        panels = [p for p in decode(body)["dashboard"]["panels"] if p["title"] == title]
        target = panels[0]["targets"][0]
        return (
            target["expr"].replace("$__rate_interval", "2m")
            if len(panels) == 1 and target["instant"] is True
            else None
        )
    except KeyError, TypeError, IndexError, AttributeError:
        return None


def discards(text: str) -> float | None:
    if "# TYPE cortex_build_info gauge" not in text.splitlines():
        return None
    total = 0.0
    for line in text.splitlines():
        if (
            line.startswith("cortex_discarded_samples_total{")
            and 'reason="otlp_parse_error"' in line
        ):
            try:
                value = float(line.split().pop())
            except ValueError:
                return None
            if not math.isfinite(value) or value < 0:
                return None
            total += value
    return total


def sum_point(
    name: str, temporality: int, value: int, times: tuple[int, int], run: str
) -> bytes:
    start, end = times
    return json.dumps(
        {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "o11y-smoke-delta"},
                            }
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "o11y-smoke"},
                            "metrics": [
                                {
                                    "name": name,
                                    "sum": {
                                        "aggregationTemporality": temporality,
                                        "isMonotonic": True,
                                        "dataPoints": [
                                            {
                                                "asInt": str(value),
                                                "startTimeUnixNano": str(
                                                    start * 1000000000
                                                ),
                                                "timeUnixNano": str(end * 1000000000),
                                                "attributes": [
                                                    {
                                                        "key": "run_id",
                                                        "value": {"stringValue": run},
                                                    }
                                                ],
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    ).encode()


def logs(now: int, *, old: bool) -> bytes:
    attrs = [
        {
            "key": "service.name",
            "value": {"stringValue": "o11y-smoke-old" if old else "o11y-smoke-rate"},
        }
    ]
    if old is False:
        attrs.append({"key": "service.namespace", "value": {"stringValue": "o11y"}})
    records = [
        {"timeUnixNano": str(now * 1000000000 + i), "body": {"stringValue": text}}
        for i, text in enumerate(("too old",) if old else ("rate one", "rate two"))
    ]
    return json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {"attributes": attrs},
                    "scopeLogs": [
                        {"scope": {"name": "o11y-smoke"}, "logRecords": records}
                    ],
                }
            ]
        }
    ).encode()


def healthy_rules(
    body: bytes, titles: tuple[str, ...], *, capacity: bool = False
) -> bool:
    try:
        rules = [
            r
            for g in decode(body)["data"]["groups"]
            if not capacity or g["name"] == "agent-capacity"
            for r in g["rules"]
        ]
        return sorted(r["name"] for r in rules) == sorted(titles) and all(
            r["health"] == "ok" and (not capacity or r["state"] == "inactive")
            for r in rules
        )
    except KeyError, TypeError:
        return False


def webhook(text: str, token: str) -> bool:
    return f"path=/12345/{token}" in text.splitlines() and any(
        isinstance(data := decode(line.encode()), dict)
        and isinstance(data.get("embeds"), list)
        and len(data["embeds"]) >= 1
        for line in text.splitlines()
        if "[FIRING:1] Disk free below the reserve" in line
    )


def mailed(body: bytes) -> bool:
    try:
        return any(
            m["To"][0]["Address"] == "smoke-to@example.invalid"
            and m["From"]["Address"] == "smoke-from@example.invalid"
            and "[FIRING:1] Disk free below the reserve" in m["Subject"]
            for m in decode(body)["messages"]
        )
    except KeyError, TypeError, IndexError:
        return False


def email_disabled(body: bytes) -> bool:
    try:
        points: list[Document] = json.loads(body)
        point = points[0]
        return len(points) == 1 and (
            point["uid"],
            point["type"],
            point["name"],
            point["provenance"],
            point["settings"]["url"],
        ) == ("o11y-discord", "discord", "o11y", "file", "[REDACTED]")
    except KeyError, TypeError, IndexError, ValueError, UnicodeError, RecursionError:
        return False
