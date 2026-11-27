"""Slice C query classifications and capacity transformations."""

# pylint: disable=duplicate-code  # verify protocol literals independently

import copy
import json
from pathlib import Path

import pytest

from tbhbops.o11y import smoke_capacity as cap
from tbhbops.o11y import smoke_query as q
lazy from tbhbops.o11y.validate import Document

ROOT = next(p for p in Path(__file__).parents if (p / "o11y/compose.yaml").exists())
DASHBOARD = json.loads(
    (ROOT / "o11y/config/grafana/dashboards/agents/capacity.json").read_text()
)


def packed(data: object) -> bytes:
    return json.dumps(data).encode()


def vector(rows: object, status: str = "success") -> bytes:
    return packed({"status": status, "data": {"result": rows}})


def frame(
    labels: Document | None = None, values: list[object] | None = None
) -> Document:
    return {
        "schema": {
            "fields": [
                {"type": "time"},
                {
                    "type": "number",
                    "labels": json.loads("{}") if labels is None else labels,
                },
            ]
        },
        "data": {"values": [[0], [0] if values is None else values]},
    }


def response(frames: object = None) -> bytes:
    return packed(
        {
            "results": {
                "C": {"status": 200, "frames": [frame()] if frames is None else frames}
            }
        }
    )


def test_vectors_and_values() -> None:
    assert q.vector(vector(json.loads("[]"))) == json.loads("[]")
    assert q.values(vector([{"value": [1, "3"]}, {"value": [2, "4"]}])) == ["3", "4"]
    for body in (
        b"null",
        b"{}",
        b"bad",
        vector(json.loads("[]"), "error"),
        vector(None),
        vector(json.loads("{}")),
    ):
        assert q.vector(body) is None
        assert q.values(body) is None
    for rows in ([json.loads("{}")], [None], [{"value": json.loads("[]")}]):
        assert q.values(vector(rows)) is None


def test_scrape_inventory_from_ops() -> None:
    text = (ROOT / "o11y/config/alloy/config.alloy").read_text()
    assert q.scrape_limits(text) == {
        "o11y/alloy": 3000,
        "o11y/loki": 5000,
        "o11y/tempo": 3000,
        "o11y/mimir": 11000,
        "o11y/grafana": 15000,
    }
    for text in (
        "",
        'prometheus.scrape "bad" {\n}',
        'prometheus.scrape "bad" {\n"job" = "o11y/a"\n}',
    ):
        with pytest.raises(ValueError, match=r"^7: scrape-limit-config$"):
            q.scrape_limits(text)
    assert q.scrape_limits(
        'prometheus.scrape "a" {\n"job" = "a"\nsample_limit = 12\n}'
    ) == {"a": 12}


def test_up_exact_labels_and_membership() -> None:
    labels = {"job": "o11y/a", "cluster": "o11y", "namespace": "o11y"}
    assert q.all_up(vector([{"metric": labels}]), ("o11y/a",))
    for key in labels:
        changed = {**labels, key: "wrong"}
        assert not q.all_up(vector([{"metric": changed}]), ("o11y/a",))
    for body in (
        b"null",
        vector([json.loads("{}")]),
        vector([None]),
        vector(json.loads("[]")),
        vector([{"metric": labels}] * 2),
    ):
        assert not q.all_up(body, ("o11y/a",))


def rule() -> Document:
    return {
        "condition": "C",
        "data": [
            {"refId": "A", "model": {"expr": "production"}},
            {"refId": "C", "model": {"conditions": [{"evaluator": {"params": [99]}}]}},
        ],
    }


def test_eval_request_threshold_override() -> None:
    data = rule()
    assert q.evaluation_request(packed(data), None) == ("C", packed(data))
    changed = copy.deepcopy(data)
    changed["data"][1]["model"]["conditions"][0]["evaluator"]["params"][0] = 0
    assert q.evaluation_request(packed(data), 0) == ("C", packed(changed))
    for body in (
        b"null",
        b"{}",
        packed({**data, "condition": ""}),
        packed({**data, "condition": 3}),
        packed({**data, "data": json.loads("{}")}),
    ):
        assert q.evaluation_request(body, None) is None
    for changed in (
        {**data, "data": json.loads("[]")},
        {**data, "data": [{"refId": "C"}]},
        {**data, "data": [{"refId": "C", "model": {"conditions": json.loads("[]")}}]},
    ):
        assert q.evaluation_request(packed(changed), 0) is None


def test_frame_classification_and_condition_selection() -> None:
    body = response([frame({"job": "a"}, [1])])
    assert q.frames(body) == {"C": [frame({"job": "a"}, [1])]}
    assert q.numbers(body) == [{"labels": {"job": "a"}, "values": [1]}]
    assert q.evaluated(body, "C") == q.numbers(body)
    assert q.evaluated(body, "missing") is None
    both = packed(
        {
            "results": {
                "A": {"status": 200, "frames": [frame(json.loads("{}"), [9])]},
                "C": {"status": 200, "frames": [frame(json.loads("{}"), [0])]},
            }
        }
    )
    assert q.evaluated(both, "C") == [{"labels": json.loads("{}"), "values": [0]}]
    assert q.numbers(response(json.loads("[]"))) == json.loads("[]")
    result_cases: tuple[object, ...] = (
        None,
        json.loads("[]"),
        json.loads("{}"),
        {"C": None},
        {"C": json.loads("{}")},
        {"C": {"status": 500, "frames": json.loads("[]")}},
        {"C": {"status": 200, "error": "private", "frames": json.loads("[]")}},
    )
    for results in result_cases:
        body = packed({"results": results})
        assert q.frames(body) is None
        assert q.numbers(body) is None
        assert q.evaluated(body, "C") is None
    for frames in (
        [None],
        [json.loads("{}")],
        [
            {
                "schema": {"fields": [{"type": "number"}]},
                "data": {"values": json.loads("[]")},
            }
        ],
    ):
        assert q.numbers(response(frames)) is None
    f = frame()
    del f["schema"]["fields"][1]["labels"]
    assert q.numbers(response([f])) == [{"labels": json.loads("{}"), "values": [0]}]


def test_firing_exact_series_and_null_label() -> None:
    rows = [
        {"labels": {"job": "a"}, "values": [1]},
        {"labels": json.loads("{}"), "values": [1]},
        {"labels": {"job": "ignored"}, "values": [0]},
    ]
    assert q.firing(rows, "job", [None, "a"])
    assert not q.firing(rows, "job", ["a"])
    assert not q.firing(rows * 2, "job", [None, "a"])
    assert q.firing(json.loads("[]"), "job", json.loads("[]"))
    for rows in (
        None,
        [json.loads("{}")],
        [None],
        [{"values": json.loads("[]")}],
        [{"values": [1], "labels": None}],
    ):
        assert not q.firing(json.loads(packed(rows)), "job", json.loads("[]"))
    assert q.quiet(json.loads("[]"))
    assert q.quiet([{"values": [0]}, {"values": [2]}])
    for rows in (
        None,
        [json.loads("{}")],
        [None],
        [{"values": json.loads("[]")}],
        [{"values": [1]}],
    ):
        assert not q.quiet(json.loads(packed(rows)))


def test_health_panel_expression() -> None:
    p = {
        "title": "test",
        "targets": [{"instant": True, "expr": "rate(x[$__rate_interval])"}],
    }
    assert (
        q.panel_expression(
            packed({"dashboard": {"panels": [p, {"title": "other"}]}}), "test"
        )
        == "rate(x[2m])"
    )
    for data in (
        None,
        json.loads("{}"),
        {"dashboard": {"panels": json.loads("[]")}},
        {"dashboard": {"panels": [p, p]}},
        {"dashboard": {"panels": [{**p, "targets": json.loads("[]")}]}},
        {"dashboard": {"panels": [{**p, "targets": [{"instant": False}]}]}},
        {"dashboard": {"panels": [{**p, "targets": [{"instant": True, "expr": 7}]}]}},
    ):
        assert q.panel_expression(packed(data), "test") is None


def test_parse_discards_requires_mimir_exposition() -> None:
    header = "# TYPE cortex_build_info gauge\n"
    assert q.discards(header) == 0
    assert q.discards("private") is None
    assert (
        q.discards(
            header + 'other{reason="otlp_parse_error"} '
            '7\ncortex_discarded_samples_total{reason="other"} 8\n'
        )
        == 0
    )
    prefix = 'cortex_discarded_samples_total{reason="otlp_parse_error"} '
    assert q.discards(header + prefix + "1.5\n" + prefix + "2e1") == 21.5
    assert q.discards(header + prefix + "0") == 0
    assert q.discards(header + prefix + "0.5") == 0.5
    for value in ("bad", "NaN", "Inf", "-1"):
        assert q.discards(header + prefix + value) is None


def test_delta_and_log_payload_contracts() -> None:
    fixture = Path(__file__).parent / "testdata/smoke-c-payloads.json"
    actual = {
        "delta": json.loads(q.sum_point("test_delta", 1, 3, (10, 20), "fixture")),
        "old": json.loads(q.logs(1000, old=True)),
        "rate": json.loads(q.logs(2000, old=False)),
        "capacity": cap.histories(1000),
    }
    assert actual == json.loads(fixture.read_text())
    assert cap.KINDS == ("warn", "shed", "stop", "exhaustion", "stale")
    assert cap.ROWS == ["Live windows", "Projection", "Burn"]
    history = cap.histories(1000)
    for resets in (False, True):
        actual = json.loads(cap.phase(history, resets=resets))
        expected = copy.deepcopy(history)
        for resource in expected["resourceMetrics"]:
            for scope in resource["scopeMetrics"]:
                scope["metrics"] = [
                    m for m in scope["metrics"] if (m["name"] == cap.RESET) == resets
                ]
        assert actual == expected


def test_rule_health_exact_inventory() -> None:
    rules: Document = {
        "data": {
            "groups": [
                {
                    "name": "agent-capacity",
                    "rules": [{"name": "A", "state": "inactive", "health": "ok"}],
                },
                {"name": "other", "rules": [{"name": "B", "health": "ok"}]},
            ]
        }
    }
    assert q.healthy_rules(packed(rules), ("A", "B"))
    assert q.healthy_rules(packed(rules), ("A",), capacity=True)
    assert not q.healthy_rules(packed(rules), ("A",))
    assert not q.healthy_rules(packed(rules), ("A", "A", "B"))
    for key, value in (("state", "firing"), ("health", "error")):
        bad = copy.deepcopy(rules)
        bad["data"]["groups"][0]["rules"][0][key] = value
        assert not q.healthy_rules(packed(bad), ("A",), capacity=True)
    assert not q.healthy_rules(b"null", ())
    assert not q.healthy_rules(b"{}", ())


def test_webhook_exact_path_and_embeds() -> None:
    content = '{"content":"[FIRING:1] Disk free below the reserve","embeds":[{}]}'
    assert q.webhook("path=/12345/token\n" + content, "token")
    for text in (
        content,
        "path=/12345/other\n" + content,
        "path=/12345/token\n[]",
        "path=/12345/token\n[FIRING:1] Disk free below the reserve",
        "path=/12345/token\n" + content.replace("[{}]", "[]"),
        "path=/12345/token\n" + content.replace("[{}]", "{}"),
    ):
        assert not q.webhook(text, "token")


def test_email_delivery_and_transition() -> None:
    message = {
        "To": [{"Address": "smoke-to@example.invalid"}],
        "From": {"Address": "smoke-from@example.invalid"},
        "Subject": "prefix [FIRING:1] Disk free below the reserve",
    }
    assert q.mailed(packed({"messages": [message]}))
    for data in (
        None,
        json.loads("{}"),
        {"messages": json.loads("[]")},
        {"messages": [None]},
        {"messages": [{**message, "To": json.loads("[]")}]},
        {"messages": [{**message, "To": [{"Address": "wrong"}]}]},
        {"messages": [{**message, "From": {"Address": "wrong"}}]},
        {"messages": [{**message, "Subject": "wrong"}]},
    ):
        assert not q.mailed(packed(data))
    point = {
        "uid": "o11y-discord",
        "type": "discord",
        "name": "o11y",
        "provenance": "file",
        "settings": {"url": "[REDACTED]"},
    }
    assert q.email_disabled(packed([point]))
    for data in (None, json.loads("{}"), json.loads("[]"), [None], [point, point]):
        assert not q.email_disabled(packed(data))
    for key in ("uid", "type", "name", "provenance", "settings"):
        assert not q.email_disabled(
            packed([{**point, key: json.loads("{}") if key == "settings" else "wrong"}])
        )


def panel() -> Document:
    return next(p for p in DASHBOARD["panels"] if p["id"] == 110)


def capacity_response(refs: str, nan: str = "") -> bytes:
    return packed(
        {
            "results": {
                ref: {
                    "status": 200,
                    "frames": [
                        frame(
                            {
                                "agent_plan_vendor": "anthropic",
                                "agent_account": "primary",
                                "agent_plan_window": "other",
                                "capacity_key": "anthropic|other|primary",
                            },
                            [None if ref in nan else 0],
                        )
                    ]
                    if ref in refs
                    else json.loads("[]"),
                }
                for ref in "ABCD"
            }
        }
    )


def test_capacity_mirror_never_invents_columns() -> None:
    assert cap.live_windows(capacity_response("A"), panel()) == [
        {"Account": "primary", "Vendor": "anthropic", "Window": "other"}
    ]
    assert cap.live_windows(capacity_response("ABD"), panel()) == [
        {
            "Account": "primary",
            "Vendor": "anthropic",
            "Window": "other",
            "Utilization": 0,
            "Projected at reset": 0,
        }
    ]
    assert cap.live_windows(capacity_response(""), panel()) == json.loads("[]")
    assert cap.live_windows(capacity_response("ABCD", "BCD"), panel()) == [
        {
            "Account": "primary",
            "Vendor": "anthropic",
            "Window": "other",
            "Utilization": None,
            "Time to reset": None,
            "Projected at reset": None,
        }
    ]
    assert cap.live_windows(capacity_response("ABCD", "C"), panel()) == [
        {
            "Account": "primary",
            "Vendor": "anthropic",
            "Window": "other",
            "Utilization": 0,
            "Time to reset": None,
            "Projected at reset": 0,
        }
    ]


def test_capacity_panel_request_and_failures() -> None:
    prepared = cap.panel_request(packed({"dashboard": DASHBOARD}), "Live windows", 1000)
    assert prepared is not None
    definition, body = prepared
    assert definition == panel()
    assert json.loads(body) == {
        "from": "700000",
        "to": "1000000",
        "queries": [
            {
                **t,
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "intervalMs": 60000,
                "maxDataPoints": 100,
            }
            for t in panel()["targets"]
        ],
    }
    assert cap.panel_request(b"null", "Live windows", 1000) is None
    for panels in (json.loads("[]"), DASHBOARD["panels"] + [panel()]):
        assert (
            cap.panel_request(
                packed({"dashboard": {"panels": panels}}), "Live windows", 1000
            )
            is None
        )
    assert cap.panel_request(packed({"dashboard": DASHBOARD}), "missing", 1000) is None


def test_capacity_table_rejections_and_outer_join() -> None:
    assert cap.live_windows(b"null", panel()) is None
    for change in ("join", "mode", "organize", "shape"):
        p = copy.deepcopy(panel())
        if change == "shape":
            p["transformations"] = json.loads("[]")
        elif change == "mode":
            p["transformations"][0]["options"]["mode"] = "inner"
        else:
            p["transformations"][0 if change == "join" else 1]["id"] = "wrong"
        assert cap.live_windows(capacity_response("ABCD"), p) is None
    for labels in (json.loads("{}"), {"capacity_key": ""}, {"capacity_key": 4}):
        body = packed(
            {"results": {"A": {"status": 200, "frames": [frame(labels, [0])]}}}
        )
        assert cap.live_windows(body, panel()) is None
    for frames in (
        [None],
        [json.loads("{}")],
        [
            {
                "schema": {"fields": [{"type": "number"}]},
                "data": {"values": json.loads("[]")},
            }
        ],
    ):
        assert (
            cap.live_windows(
                packed({"results": {"A": {"status": 200, "frames": frames}}}), panel()
            )
            is None
        )
    # A later target supplies an inventory key absent from the first target.
    body = json.loads(capacity_response("ABCD"))
    body["results"]["B"]["frames"].append(
        frame(
            {
                "agent_account": "primary",
                "capacity_key": "openai|5h|primary",
                "agent_plan_vendor": "openai",
                "agent_plan_window": "5h",
            },
            [0.25],
        )
    )
    joined = cap.live_windows(packed(body), panel())
    assert joined is not None
    assert joined[1] == {
        "Account": "primary",
        "Vendor": "openai",
        "Window": "5h",
        "Utilization": 0.25,
        "Time to reset": None,
        "Projected at reset": None,
    }
    minimal = copy.deepcopy(panel())
    minimal["transformations"][1]["options"] = json.loads("{}")
    first = cap.live_windows(capacity_response("A"), minimal)
    assert first == [
        {
            "capacity_key": "anthropic|other|primary",
            "agent_plan_vendor": "anthropic",
            "agent_plan_window": "other",
            "agent_account": "primary",
            "Value #A": 0,
        }
    ]
    del body["results"]["A"]
    assert cap.live_windows(packed(body), panel()) is not None


def windows() -> list[Document]:
    return [
        {
            "Account": "primary",
            "Vendor": v,
            "Window": w,
            "Utilization": u,
            "Time to reset": t,
            "Projected at reset": p,
        }
        for v, w, u, t, p in (
            ("anthropic", "5h", 0.7, 3600, None),
            ("anthropic", "7d", 0.888, 172800, None),
            ("anthropic", "7d_sonnet", 0, 86400, None),
            ("google", "other", None, None, None),
            ("openai", "5h", 0.25, 7200, None),
        )
    ]


def test_capacity_readiness_every_reset_countdown() -> None:
    rows = windows()
    assert cap.windows(rows, "ready")
    assert not cap.windows(rows, "unknown")
    boundary = windows()
    for r in boundary:
        if r["Window"] != "other":
            r["Time to reset"] = 1
    assert cap.windows(boundary, "ready")
    assert not cap.windows([*windows(), windows()[0]], "ready")
    assert not cap.windows([{**r, "extra": 1} for r in windows()], "ready")
    for i in (0, 1, 2, 4):
        bad = copy.deepcopy(rows)
        bad[i]["Time to reset"] = None
        assert not cap.windows(bad, "ready")
        bad[i]["Time to reset"] = 0
        assert not cap.windows(bad, "ready")
    for i, column, value in (
        (0, "Utilization", 0.99),
        (0, "Projected at reset", 0.99),
        (1, "Utilization", 0.88),
        (1, "Projected at reset", 0.99),
        (3, "Utilization", 0),
    ):
        bad = copy.deepcopy(rows)
        bad[i][column] = value
        assert not cap.windows(bad, "ready")
    for rows in (
        None,
        json.loads("[]"),
        [json.loads("{}")] * 5,
        windows()[:-1],
        [windows()[0]] * 5,
    ):
        assert not cap.windows(rows, "ready")
    rows = windows()
    for r in rows:
        r["Time to reset"] = None
    assert cap.windows(rows, "missing-reset")
    rows[4]["Utilization"] = 0.26
    assert not cap.windows(rows, "missing-reset")
    for r in rows:
        for column in cap.NUMERIC_COLUMNS:
            r[column] = None
    assert cap.windows(rows, "inventory")
    rows[0]["Utilization"] = 0
    assert not cap.windows(rows, "inventory")


def ingested() -> list[Document]:
    labels = [
        {
            "__name__": name,
            "agent_plan_vendor": vendor,
            "host_name": "capacity-fixture-a",
            "agent_plan_window": window,
            "agent_account": "primary",
            "agent_capacity_source": source,
            "agent_capacity_freshness": freshness,
        }
        for name, vendor, window, source, freshness in [
            (cap.UTIL, "anthropic", "5h", "claude_statusline", "carried"),
            (cap.UTIL, "anthropic", "7d", "claude_statusline", "fresh"),
            (cap.UTIL, "anthropic", "7d_sonnet", "codex_rollout", "carried"),
            (cap.UTIL, "openai", "5h", "claude_statusline", "fresh"),
            (cap.UTIL, "anthropic", "5h", "claude_statusline", "fresh"),
            (cap.RESET, "anthropic", "5h", "claude_statusline", "fresh"),
            (cap.AGE, "anthropic", "5h", "claude_statusline", "fresh"),
        ]
    ]
    return [{"metric": m} for m in labels]


LOOKBACK = 300
PREFIX = "agent_capacity_"  # the smoke queries {__name__=~"agent_capacity_.*"}


def attributes(items: list[Document]) -> dict[str, str]:
    return {a["key"]: a["value"]["stringValue"] for a in items}


def resource_points(resource: Document) -> list[tuple[int, Document]]:
    base = {
        k.replace(".", "_"): v
        for k, v in attributes(resource["resource"]["attributes"]).items()
    }
    return [
        (
            int(dp["timeUnixNano"]) // 10**9,
            {"__name__": metric["name"], **base, **attributes(dp["attributes"])},
        )
        for scope in resource["scopeMetrics"]
        for metric in scope["metrics"]
        for dp in metric["gauge"]["dataPoints"]
    ]


def emitted_points(now: int) -> list[tuple[int, Document]]:
    """Every gauge point the smoke uploads, with resource labels merged in."""
    history = cap.histories(now)
    return [
        point
        for resets in (False, True)
        for resource in json.loads(cap.phase(history, resets=resets))["resourceMetrics"]
        for point in resource_points(resource)
    ]


def emitted_rows() -> list[Document]:
    """The instant vector Mimir returns for the uploaded points."""
    now = 1_700_000_000
    seen: dict[str, Document] = {}
    for seconds, labels in emitted_points(now):
        # points outside the instant query's staleness window never come back
        if seconds >= now - LOOKBACK and labels["__name__"].startswith(PREFIX):
            seen[json.dumps(labels, sort_keys=True)] = {"metric": labels}
    return list(seen.values())


def test_capacity_ingestion_accepts_the_emitted_series() -> None:
    now = 1_700_000_000
    assert min(seconds for seconds, _ in emitted_points(now)) == now - 1800
    rows = emitted_rows()
    assert {r["metric"]["__name__"] for r in rows} == {
        cap.UTIL,
        cap.RESET,
        cap.AGE,
    }
    old_prefix = "af" + "akt"
    assert not any(old_prefix in name for r in rows for name in r["metric"])
    assert cap.ingested(vector(rows))


def test_capacity_ingestion_exact_names_and_labels() -> None:
    rows = ingested()
    assert cap.ingested(vector(rows))
    for key, value in (
        ("__name__", "wrong"),
        ("agent_plan_vendor", "wrong"),
        ("agent_account", "wrong"),
        ("host_name", "wrong"),
        ("agent_plan_window", "wrong"),
        ("agent_capacity_source", "wrong"),
        ("agent_capacity_freshness", "wrong"),
    ):
        bad = copy.deepcopy(rows)
        bad[0]["metric"][key] = value
        assert not cap.ingested(vector(bad))
    for body in (
        b"null",
        vector(json.loads("[]")),
        vector([None]),
        vector([{"metric": json.loads("{}")}]),
        vector(
            [
                {
                    "metric": {
                        "__name__": cap.UTIL,
                        "agent_plan_vendor": "anthropic",
                        "host_name": None,
                    }
                }
            ]
        ),
    ):
        assert not cap.ingested(body)


@pytest.mark.parametrize(
    ("rows", "text"),
    [
        (None, "no usable result"),
        ([], "[]"),
        (
            [
                {"labels": {"job": "z", "other": "private"}, "values": [1, 0]},
                {"labels": {"job": "a"}, "values": [1]},
                {"labels": {"job": "z"}, "values": [1]},
                {"labels": {}, "values": [1]},
                {"labels": {"job": "ignored"}, "values": [0, 1]},
                {"labels": {"job": "also-ignored"}, "values": [2]},
            ],
            "[None, 'a', 'z', 'z']",
        ),
        ([{}], "malformed row"),
        ([None], "malformed row"),
        ([{"values": []}], "malformed row"),
        ([{"values": [1], "labels": None}], "malformed row"),
    ],
)
def test_observed_labels(rows: list[Document] | None, text: str) -> None:
    assert q.observed_labels(rows, "job") == text
