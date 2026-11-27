"""Capacity histories and Grafana's configured table transformations."""

import json

from tbhbops.o11y.receipt import decode
from tbhbops.o11y.smoke_query import frames, vector
lazy from tbhbops.o11y.validate import Document

UTIL = "agent_capacity_window_utilization_ratio"
RESET = "agent_capacity_window_reset_time_seconds"
AGE = "agent_capacity_snapshot_age_seconds"
ROWS = ["Live windows", "Projection", "Burn"]
COLUMNS = [
    "Account",
    "Projected at reset",
    "Time to reset",
    "Utilization",
    "Vendor",
    "Window",
]
WINDOW_COUNT = 5
STALE_INDEX = 3
OPENAI_USAGE = 0.25
WEEKLY_USAGE_FLOOR = 0.88
NUMERIC_COLUMNS = ("Projected at reset", "Time to reset", "Utilization")
KINDS = ("warn", "shed", "stop", "exhaustion", "stale")


def attrs(vendor: str, window: str, source: str, freshness: str) -> list[Document]:
    return [
        {"key": key, "value": {"stringValue": value}}
        for key, value in {
            "agent_account": "primary",
            "agent_plan_vendor": vendor,
            "agent_plan_window": window,
            "agent_capacity_source": source,
            "agent_capacity_freshness": freshness,
        }.items()
    ]


def point(labels: list[Document], timestamp: int, value: float) -> Document:
    return {
        "attributes": labels,
        "timeUnixNano": str(timestamp * 1000000000),
        "asDouble": value,
    }


def histories(now: int) -> Document:
    labels = [
        attrs("anthropic", "7d", "claude_statusline", "fresh"),
        attrs("anthropic", "5h", "claude_statusline", "fresh"),
        attrs("anthropic", "7d_sonnet", "claude_statusline", "carried"),
        attrs("google", "other", "agy_statusline", "carried"),
        attrs("openai", "5h", "codex_rollout", "fresh"),
    ]
    metrics = {
        UTIL: [
            point(labels[0], now - 1800 + i * 300, 0.864 + i * 0.004) for i in range(7)
        ]
        + [
            point(labels[1], now, 0.7),
            point(labels[2], now, 0),
            point(labels[3], now - 1200, 0),
            point(labels[4], now, 0.25),
        ],
        AGE: [
            point(
                label,
                now - (1200 if i == STALE_INDEX else 0),
                1200 if i == STALE_INDEX else 0,
            )
            for i, label in enumerate(labels)
        ],
        RESET: [
            point(labels[i], now - (1200 if i == STALE_INDEX else 0), now + duration)
            for i, duration in enumerate((172800, 3600, 86400, 3600, 7200))
        ],
        **{
            name: [
                point(
                    [{"key": "agent_account", "value": {"stringValue": "primary"}}],
                    now - 300 + i * 60,
                    (i + 1) * 60,
                )
                for i in range(6)
            ]
            for name in (
                "claude_code_token_usage_tokens_total",
                "claude_code_cost_usage_USD_total",
            )
        },
        "codex_turn_token_usage_sum": [
            point(
                [
                    {"key": "token_type", "value": {"stringValue": kind}},
                    {"key": "agent_account", "value": {"stringValue": "primary"}},
                ],
                now - 300 + i * 60,
                (i + 1) * scale,
            )
            for kind, scale in (("total", 60), ("input", 30))
            for i in range(6)
        ],
    }
    resources = []
    for host, data in (
        ("capacity-fixture-a", metrics),
        (
            "capacity-fixture-b",
            {
                UTIL: [
                    point(
                        attrs("anthropic", "5h", "claude_statusline", "carried"),
                        now,
                        1,
                    )
                ],
                AGE: [
                    point(
                        attrs("anthropic", "5h", "claude_statusline", "carried"),
                        now,
                        60,
                    )
                ],
                RESET: [
                    point(
                        attrs("anthropic", "5h", "claude_statusline", "carried"),
                        now,
                        now + 3600,
                    )
                ],
            },
        ),
    ):
        resources.append(
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "agent-capacity"},
                        },
                        {"key": "host.name", "value": {"stringValue": host}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "capacity-smoke"},
                        "metrics": [
                            {"name": name, "gauge": {"dataPoints": points}}
                            for name, points in data.items()
                        ],
                    }
                ],
            }
        )
    return {"resourceMetrics": resources}


def phase(history: Document, *, resets: bool) -> bytes:
    return json.dumps(
        {
            "resourceMetrics": [
                {
                    **r,
                    "scopeMetrics": [
                        {
                            **s,
                            "metrics": [
                                m
                                for m in s["metrics"]
                                if (m["name"] == RESET) == resets
                            ],
                        }
                        for s in r["scopeMetrics"]
                    ],
                }
                for r in history["resourceMetrics"]
            ]
        }
    ).encode()


def panel_request(body: bytes, title: str, now: int) -> tuple[Document, bytes] | None:
    try:
        panels = decode(body)["dashboard"]["panels"]
        if [p["title"] for p in panels if p["type"] == "row"] != ROWS:
            return None
        selected = [p for p in panels if p["type"] != "row" and p["title"] == title]
        if len(selected) != 1:
            return None
        panel = selected[0]
        request = {
            "from": str((now - 300) * 1000),
            "to": str(now * 1000),
            "queries": [
                {
                    **t,
                    "datasource": {"type": "prometheus", "uid": "mimir"},
                    "intervalMs": 60000,
                    "maxDataPoints": 100,
                }
                for t in panel["targets"]
            ],
        }
        return panel, json.dumps(request).encode()
    except KeyError, TypeError:
        return None


def _table_rows(group: list[Document], ref: str, by: str) -> list[Document]:
    rows = []
    for frame in group:
        for i, field in enumerate(frame["schema"]["fields"]):
            if field["type"] == "number":
                row = {
                    **field["labels"],
                    "Value #" + ref: frame["data"]["values"][i][0],
                }
                if not isinstance(row.get(by), str) or not row[by]:
                    raise ValueError
                rows.append(row)
    return rows


def _joined(
    tables: list[tuple[str, list[Document]]],
    by: str,
    exclude: Document,
    rename: Document,
) -> list[Document]:
    keys = sorted({r[by] for _, rows in tables for r in rows})
    joined = []
    for key in keys:
        base = next(r for _, rows in tables for r in rows if r[by] == key).copy()
        for ref, rows in tables[1:]:
            base["Value #" + ref] = next(
                (r["Value #" + ref] for r in rows if r[by] == key), None
            )
        joined.append(
            {rename.get(k, k): v for k, v in base.items() if not exclude.get(k)}
        )
    return joined


def live_windows(body: bytes, panel: Document) -> list[Document] | None:
    try:
        results = frames(body)
        if results is None:
            return None
        join, organize = panel["transformations"]
        if (
            join["id"] != "joinByField"
            or join["options"]["mode"] != "outerTabular"
            or organize["id"] != "organize"
        ):
            return None
        by = join["options"]["byField"]
        exclude = organize["options"].get("excludeByName", {})
        rename = organize["options"].get("renameByName", {})
        tables = []
        for target in panel["targets"]:
            ref = target["refId"]
            rows = _table_rows(results.get(ref, []), ref, by)
            if rows:
                tables.append((ref, rows))
        return _joined(tables, by, exclude, rename)
    except KeyError, TypeError, IndexError, ValueError, AttributeError:
        return None


def ingested(body: bytes) -> bool:
    try:
        rows = vector(body)
        if rows is None:
            return False
        labels = [r["metric"] for r in rows]
        return (
            sorted({m["__name__"] for m in labels}) == sorted((UTIL, RESET, AGE))
            and sorted({m["agent_plan_vendor"] for m in labels})
            == ["anthropic", "openai"]
            and all(m["agent_account"] == "primary" for m in labels)
            and all(m["host_name"].startswith("capacity-fixture-") for m in labels)
            and len([m for m in labels if m["__name__"] == UTIL]) == WINDOW_COUNT
            and any(
                m.get("agent_plan_window") == "5h"
                and m.get("agent_capacity_source") == "claude_statusline"
                and m.get("agent_capacity_freshness") == "carried"
                for m in labels
            )
        )
    except KeyError, TypeError, AttributeError:
        return False


def windows(rows: list[Document] | None, mode: str) -> bool:
    try:
        if (
            mode not in {"inventory", "missing-reset", "ready"}
            or rows is None
            or len(rows) != WINDOW_COUNT
            or any(sorted(r) != COLUMNS or r["Account"] != "primary" for r in rows)
        ):
            return False
        if mode == "inventory":
            return all(r[c] is None for r in rows for c in NUMERIC_COLUMNS)
        indexed = {(r["Vendor"], r["Window"]): r for r in rows}
        if set(indexed) != {("anthropic", w) for w in ("5h", "7d", "7d_sonnet")} | {
            ("openai", "5h"),
            ("google", "other"),
        }:
            return False
        if mode == "missing-reset":
            return (
                all(r["Time to reset"] is None for r in rows)
                and indexed["anthropic", "7d_sonnet"]["Utilization"] == 0
                and indexed["openai", "5h"]["Utilization"] == OPENAI_USAGE
            )
        expectations = (
            (("anthropic", "5h"), 0.7),
            (("openai", "5h"), 0.25),
            (("anthropic", "7d_sonnet"), 0),
        )
        correct = all(
            indexed[key]["Utilization"] == usage
            and indexed[key]["Projected at reset"] is None
            and indexed[key]["Time to reset"] > 0
            for key, usage in expectations
        )
        weekly = indexed["anthropic", "7d"]
        stale = indexed["google", "other"]
        return (
            correct
            and weekly["Utilization"] > WEEKLY_USAGE_FLOOR
            and weekly["Projected at reset"] is None
            and weekly["Time to reset"] > 0
            and all(stale[c] is None for c in NUMERIC_COLUMNS)
        )
    except KeyError, TypeError:
        return False
