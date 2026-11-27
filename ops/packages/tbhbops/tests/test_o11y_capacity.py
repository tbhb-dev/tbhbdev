"""Check capacity dashboards and alert rules against the live ops config."""

import copy
import json
import math
import os
import subprocess
from pathlib import Path
lazy from collections.abc import Callable

import pytest
import yaml
from tbhbops.o11y import smoke_capacity
lazy from tbhbops.o11y.validate import Document

ROOT = next(p for p in Path(__file__).parents if (p / "o11y/compose.yaml").exists())
CONFIG = ROOT / "o11y/config/grafana"
DASHBOARD = json.loads((CONFIG / "dashboards/agents/capacity.json").read_text())
RULES = yaml.safe_load((CONFIG / "provisioning/alerting/rules.yaml").read_text())
GROUP = next(g for g in RULES["groups"] if g["name"] == "agent-capacity")
PANELS = {p["id"]: p for p in DASHBOARD["panels"]}
UTIL = "agent_capacity_window_utilization_ratio"
RESET = "agent_capacity_window_reset_time_seconds"
AGE = "agent_capacity_snapshot_age_seconds"
SOURCE = (
    'agent_capacity_source=~"claude_statusline|claude_transcript|'
    'codex_rollout|codex_probe|agy_statusline"'
)
FRESH = "{" + SOURCE + ',agent_capacity_freshness=~"fresh|carried"}'
UTIL_FRESH = UTIL + FRESH
CAPACITY_KEYS = "agent_plan_vendor, agent_plan_window, agent_account, agent_plan_bucket"


def preferred_utilization(lookback: str = "", labels: str = CAPACITY_KEYS) -> str:
    read = UTIL_FRESH
    if lookback:
        read = "last_over_time(" + read + "[" + lookback + "])"
    return (
        "(max by ("
        + labels
        + ") ("
        + read.replace(
            'agent_capacity_freshness=~"fresh|carried"',
            'agent_capacity_freshness="fresh"',
        )
        + ") or max by ("
        + labels
        + ") ("
        + read.replace(
            'agent_capacity_freshness=~"fresh|carried"',
            'agent_capacity_freshness="carried"',
        )
        + "))"
    )


KEYS = {
    "agent_plan_vendor": "anthropic",
    "agent_plan_window": "7d",
    "agent_account": "primary",
}


def test_capacity_dashboard_identity() -> None:
    assert DASHBOARD["uid"] == "o11y-agents-capacity"
    assert DASHBOARD["title"] == "Agent capacity"
    assert [p["title"] for p in DASHBOARD["panels"] if p["type"] == "row"] == [
        "Live windows",
        "Projection",
        "Burn",
    ]
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert ids == [100, 110, 112, 114, 101, 120, 102, 130, 131, 132, 140]
    assert len(ids) == len(set(ids))
    assert any(
        link["type"] == "dashboards"
        and link["tags"] == ["agents"]
        and link["keepTime"]
        and link["includeVars"]
        for link in DASHBOARD["links"]
    )


def test_capacity_five_minute_horizon() -> None:
    for target in [*PANELS[110]["targets"][1:], PANELS[112]["targets"][0]]:
        assert target["instant"] is True
        assert ("[5m]" if target in PANELS[110]["targets"] else "[15m]") in target[
            "expr"
        ]
        assert "[24h]" not in target["expr"]
    for panel in (PANELS[110], PANELS[120]):
        for target in panel["targets"]:
            assert (
                "max by (agent_plan_vendor, agent_plan_window, agent_account)"
                in target["expr"]
            )
            assert 'agent_capacity_freshness="fresh"' in target["expr"]
            assert 'agent_capacity_freshness="carried"' in target["expr"]
    metadata = PANELS[112]
    assert metadata["type"] == metadata["targets"][0]["format"] == "table"
    assert [t["id"] for t in metadata["transformations"]] == [
        "labelsToFields",
        "merge",
        "organize",
    ]
    assert metadata["transformations"][0]["options"]["mode"] == "columns"
    options = metadata["transformations"][2]["options"]
    assert options["renameByName"]["agent_account"] == "Account"
    assert options["renameByName"]["agent_capacity_source"] == "Source"
    assert options["renameByName"]["agent_capacity_freshness"] == "Freshness"
    assert options["excludeByName"]["Value"] is True
    assert PANELS[120]["fieldConfig"]["defaults"]["noValue"] == "unknown"


def test_capacity_six_hour_inventory_join() -> None:
    panel = PANELS[110]
    assert panel["title"] == "Live windows"
    assert panel["type"] == "table"
    assert [t["refId"] for t in panel["targets"]] == ["A", "B", "C", "D"]
    suffix = (
        ', "capacity_key", "|", "agent_plan_vendor", '
        '"agent_plan_window", "agent_account")'
    )
    for target in panel["targets"]:
        assert target["instant"] is True
        assert target["range"] is False
        assert target["format"] == "table"
        assert target["expr"].startswith("label_join(")
        assert target["expr"].endswith(suffix)
    assert (
        panel["targets"][0]["expr"]
        == ("label_join(max by (agent_plan_vendor, agent_plan_window, agent_account) (")
        + preferred_utilization("6h")
        + ")"
        + suffix
    )
    assert 'agent_capacity_freshness="fresh"}[5m])' in panel["targets"][1]["expr"]
    reset = panel["targets"][2]["expr"]
    assert "agent_plan_bucket" in reset
    assert "last_over_time(" + RESET in reset
    assert "last_over_time(" + UTIL in reset
    assert ")) - time()" in reset
    assert "[6h:5m]) > 0" in panel["targets"][3]["expr"]
    assert "[24h:5m]) > 0" in panel["targets"][3]["expr"]
    assert "[6h:5m]) >= 36" in panel["targets"][3]["expr"]
    assert "[24h:5m]) >= 144" in panel["targets"][3]["expr"]
    assert [t["id"] for t in panel["transformations"]] == ["joinByField", "organize"]
    assert panel["transformations"][0]["options"] == {
        "byField": "capacity_key",
        "mode": "outerTabular",
    }
    options = panel["transformations"][1]["options"]
    assert options["excludeByName"]["Value #A"] is True
    assert options["excludeByName"]["capacity_key"] is True
    assert options["renameByName"]["agent_account"] == "Account"
    for key in (
        "agent_plan_vendor",
        "agent_plan_vendor 1",
        "agent_plan_window",
        "agent_plan_window 1",
    ):
        assert not options["excludeByName"].get(key, False)
        assert options["renameByName"][key] == (
            "Vendor" if "vendor" in key else "Window"
        )
    for ref, name in (
        ("B", "Utilization"),
        ("C", "Time to reset"),
        ("D", "Projected at reset"),
    ):
        assert options["renameByName"]["Value #" + ref] == name
    assert "noValue" not in panel["fieldConfig"]["defaults"]
    assert [
        o["matcher"]
        for o in panel["fieldConfig"]["overrides"]
        if {"id": "noValue", "value": "unknown"} in o["properties"]
    ] == [
        {"id": "byName", "options": name}
        for name in ("Utilization", "Time to reset", "Projected at reset")
    ]


def test_capacity_snapshot_age_contract() -> None:
    panel = PANELS[114]
    assert panel["type"] == "stat"
    assert panel["fieldConfig"]["defaults"] == {"unit": "s", "noValue": "unknown"}
    target = panel["targets"][0]
    assert target["instant"] is True
    assert target["range"] is False
    assert target["legendFormat"] == "{{agent_plan_vendor}} / {{agent_account}}"
    assert target["expr"] == (
        "max by (agent_plan_vendor, agent_account) (" + AGE + "{" + SOURCE + "})"
    )
    assert AGE in panel["description"]
    assert DASHBOARD["version"] == 6
    for panel in DASHBOARD["panels"]:
        for target in panel.get("targets", []):
            assert all(label not in target["expr"] for label in ("instance", "job"))
            assert ("host_name" in target["expr"]) == (panel["id"] == 132)


def test_capacity_history_contract() -> None:
    panel = PANELS[140]
    assert panel["title"] == "Utilization history by account and window"
    assert panel["type"] == "timeseries"
    assert panel["timeFrom"] == "7d"
    assert panel["targets"] == [
        {
            "refId": "A",
            "datasource": {"type": "prometheus", "uid": "mimir"},
            "expr": (
                "max by (agent_plan_vendor, agent_account, agent_plan_window, "
                "agent_plan_bucket) (" + preferred_utilization() + ")"
            ),
            "legendFormat": (
                "{{agent_plan_vendor}} {{agent_account}} {{agent_plan_window}} "
                "{{agent_plan_bucket}}"
            ),
        }
    ]


def test_capacity_alert_contract() -> None:
    assert [r["title"] for r in GROUP["rules"]] == [
        "AgentPlanWindowWarn",
        "AgentPlanWindowShed",
        "AgentPlanWindowStop",
        "Weekly capacity exhaustion",
        "AgentPlanCaptureStale",
    ]
    for rule in GROUP["rules"]:
        identity = {"agent_plan_vendor", "agent_account"}
        if rule["uid"] != "o11y-capacity-stale":
            identity.add("agent_plan_window")
        expr = rule["data"][0]["model"]["expr"]
        assert all(label in expr for label in identity)
        assert set(rule["labels"]) | identity | {"alertname"} == {
            "alertname",
            "service",
            "severity",
            "env",
            "agent_plan_vendor",
            "agent_plan_window",
            "agent_account",
        }
        assert rule["annotations"] == {
            "used_share": "{{ $values.U.Value }}",
            "reset_time_seconds": "{{ $values.R.Value }}",
            "snapshot_age_seconds": "{{ $values.S.Value }}",
        }
        for node in rule["data"][:-1]:
            assert all(
                label not in node["model"]["expr"]
                for label in ("host_name", "instance", "job")
            )


def test_capacity_queries_pin_source() -> None:
    exprs: list[str] = [
        t["expr"] for panel in DASHBOARD["panels"] for t in panel.get("targets", [])
    ]
    exprs += [n["model"]["expr"] for r in GROUP["rules"] for n in r["data"][:-1]]
    selectors = 0
    for expr in exprs:
        flat: str = " ".join(expr.split())
        for metric in (UTIL, RESET, AGE):
            parts: list[str] = flat.split(metric)[1:]
            for part in parts:
                selectors += 1
                assert part.startswith("{"), flat
                assert SOURCE in part.split("}")[0], flat
    assert selectors > len(exprs)


def test_capacity_codex_burn_total_only() -> None:
    assert (
        'codex_turn_token_usage_sum{token_type="total"}'
        in PANELS[132]["targets"][0]["expr"]
    )


def test_capacity_burn_legends() -> None:
    for identifier in (130, 131):
        target = PANELS[identifier]["targets"][0]
        assert target["legendFormat"] == "fleet total"
        assert target["expr"].startswith("sum(rate(")
    target = PANELS[132]["targets"][0]
    assert target["legendFormat"] == "Codex tokens / {{agent_account}}"
    assert target["expr"].startswith(
        "max by (agent_account) (sum by (host_name, agent_account) (rate("
    )


def test_capacity_inventory_nan_unknown() -> None:
    fallback = (
        "or on (agent_plan_vendor, agent_plan_window, agent_account) (max by "
        "(agent_plan_vendor, agent_plan_window, agent_account) ("
        + preferred_utilization("6h")
        + ") * NaN)"
    )
    assert all(fallback in t["expr"] for t in PANELS[110]["targets"][1:])
    for override in PANELS[110]["fieldConfig"]["overrides"]:
        assert {
            "id": "mappings",
            "value": [
                {
                    "type": "special",
                    "options": {"match": "nan", "result": {"text": "unknown"}},
                }
            ],
        } in override["properties"]


def check_promtool(
    directory: Path, document: Document, *, rules: list[Document] | None = None
) -> None:
    if rules is not None:
        group = {key: GROUP[key] for key in ("name", "interval")}
        (directory / "rules.yaml").write_text(
            yaml.safe_dump({"groups": [{**group, "rules": rules}]}),
            encoding="utf-8",
        )
        document = {
            **document,
            "rule_files": ["rules.yaml"],
            "evaluation_interval": group["interval"],
        }
    test_file = directory / "tests.yaml"
    test_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    binary = os.environ.get("TBHB_PROMTOOL")
    promtool = Path(os.environ["TEST_SRCDIR"], binary) if binary else "promtool"
    result = subprocess.run(  # nosec B603: pinned promtool, argv only
        [promtool, "test", "rules", test_file],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def capacity_target_check(
    case: tuple[str, str, str, float, str],
    target: Document,
) -> Document:
    name, vendor, window, value, time = case
    expr = target["expr"]
    nan = name == "inventory only" or target["refId"] in {"C", "D"}
    labels = (
        f'{{agent_plan_vendor="{vendor}",agent_plan_window="{window}",'
        f'agent_account="primary",capacity_key="{vendor}|{window}|primary"}}'
    )
    return {
        "expr": f"({expr}) != bool ({expr})" if nan else expr,
        "eval_time": time,
        "exp_samples": [{"labels": labels, "value": 1 if nan else value}],
    }


def test_capacity_targets_nan_and_missing_reset(tmp_path: Path) -> None:
    tests = []
    for case in (
        ("inventory only", "anthropic", "other", 0, "20m"),
        ("missing reset", "openai", "5h", 0.25, "1m"),
    ):
        name, vendor, window, value, _ = case
        checks = [
            capacity_target_check(case, target) for target in PANELS[110]["targets"][1:]
        ]
        tests.append(
            {
                "name": name,
                "interval": "1m",
                "input_series": [
                    {
                        "series": UTIL
                        + '{agent_plan_vendor="'
                        + vendor
                        + '",agent_plan_window="'
                        + window
                        + (
                            '",agent_capacity_source="codex_rollout",'
                            'agent_account="primary",agent_capacity_freshness="fresh"}'
                        ),
                        "values": str(value),
                    },
                    {
                        "series": AGE
                        + '{agent_plan_vendor="'
                        + vendor
                        + '",agent_plan_window="'
                        + window
                        + (
                            '",agent_capacity_source="codex_rollout",'
                            'agent_account="primary",agent_capacity_freshness="fresh"}'
                        ),
                        "values": "0",
                    },
                ],
                "promql_expr_test": checks,
            }
        )
    age = PANELS[114]["targets"][0]["expr"]
    tests.append(
        {
            "name": "source observation age retains accounts and reduces hosts",
            "interval": "1m",
            "input_series": [
                series(AGE, "60 stale"),
                series(AGE, "120 stale", host_name="fixture-b"),
                series(AGE, "30 stale", agent_account="secondary"),
            ],
            "promql_expr_test": [
                {
                    "expr": age,
                    "eval_time": "0m",
                    "exp_samples": [
                        {
                            "labels": (
                                '{agent_plan_vendor="anthropic",agent_account="primary"}'
                            ),
                            "value": 120,
                        },
                        {
                            "labels": (
                                '{agent_plan_vendor="anthropic",agent_account="secondary"}'
                            ),
                            "value": 30,
                        },
                    ],
                },
                {"expr": age, "eval_time": "1m", "exp_samples": []},
            ],
        }
    )
    # All annotation queries are evaluated independently of Grafana's threshold.
    rule = GROUP["rules"][0]
    tests.append(
        {
            "name": "numeric annotations retain vendor window identity",
            "interval": "1m",
            "input_series": [
                series(UTIL, "0.5 stale"),
                series(UTIL, "0.9x60", agent_capacity_freshness="carried"),
                series(RESET, "86400x60", agent_capacity_freshness="carried"),
            ],
            "promql_expr_test": [
                {
                    "expr": node["model"]["expr"],
                    "eval_time": "40m",
                    "exp_samples": [
                        {
                            "labels": (
                                '{agent_plan_vendor="anthropic",agent_plan_window="7d",agent_account="primary"}'
                            ),
                            "value": value,
                        }
                    ],
                }
                for node, value in zip(
                    rule["data"][1:4], (0.9, 86400, 2400), strict=True
                )
            ],
        }
    )
    tests.append(
        {
            "name": "missing reset and fresh history use numeric annotation sentinels",
            "interval": "1m",
            "input_series": [
                series(UTIL, "0.9x60", agent_capacity_freshness="carried")
            ],
            "promql_expr_test": [
                {
                    "expr": node["model"]["expr"],
                    "eval_time": "40m",
                    "exp_samples": [
                        {
                            "labels": (
                                '{agent_plan_vendor="anthropic",agent_plan_window="7d",agent_account="primary"}'
                            ),
                            "value": -1,
                        }
                    ],
                }
                for node in rule["data"][2:4]
            ],
        }
    )
    rule = GROUP["rules"][-1]
    tests.append(
        {
            "name": "stale annotations bind reset to the highest-use bucket",
            "interval": "1m",
            "input_series": [
                series(UTIL, "0.42 stale", agent_plan_bucket="weekly"),
                series(RESET, "86400 stale", agent_plan_bucket="weekly"),
                series(UTIL, "0.90 stale", agent_plan_bucket="sonnet"),
                series(RESET, "43200 stale", agent_plan_bucket="sonnet"),
            ],
            "promql_expr_test": [
                {
                    "expr": node["model"]["expr"],
                    "eval_time": "2h",
                    "exp_samples": [
                        {
                            "labels": (
                                '{agent_plan_vendor="anthropic",agent_account="primary"}'
                            ),
                            "value": value,
                        }
                    ],
                }
                for node, value in zip(
                    rule["data"][1:4], (0.9, 43200, 7200), strict=True
                )
            ],
        }
    )
    tests.append(
        {
            "name": "live table preserves separate accounts",
            "interval": "1m",
            "input_series": [
                series(UTIL, "0.95", agent_account="primary"),
                series(UTIL, "0.25", agent_account="secondary"),
                series(UTIL, "0.20", host_name="fixture-b"),
                series(AGE, "0", agent_account="primary"),
                series(AGE, "0", agent_account="secondary"),
                series(AGE, "0", host_name="fixture-b"),
            ],
            "promql_expr_test": [
                {
                    "expr": PANELS[110]["targets"][1]["expr"],
                    "eval_time": "0m",
                    "exp_samples": [
                        {
                            "labels": (
                                '{agent_plan_vendor="anthropic",agent_plan_window="7d",'
                                'agent_account="primary",capacity_key="anthropic|7d|primary"}'
                            ),
                            "value": 0.95,
                        },
                        {
                            "labels": (
                                '{agent_plan_vendor="anthropic",agent_plan_window="7d",'
                                'agent_account="secondary",'
                                'capacity_key="anthropic|7d|secondary"}'
                            ),
                            "value": 0.25,
                        },
                    ],
                }
            ],
        }
    )
    stale_labels = {
        "agent_plan_vendor": "openai",
        "agent_plan_window": "7d",
        "agent_account": "codex-primary",
    }
    tests.append(
        {
            "name": "live table shows the freshest reading per account and window",
            "interval": "1m",
            "input_series": [
                series(
                    UTIL,
                    "0.11",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    AGE,
                    "3000",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    UTIL,
                    "0.52",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
                series(
                    AGE,
                    "20",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
            ],
            "promql_expr_test": [
                {
                    "expr": PANELS[110]["targets"][1]["expr"],
                    "eval_time": "0m",
                    "exp_samples": [
                        {
                            "labels": (
                                '{agent_plan_vendor="openai",agent_plan_window="7d",'
                                'agent_account="codex-primary",'
                                'capacity_key="openai|7d|codex-primary"}'
                            ),
                            "value": 0.52,
                        }
                    ],
                }
            ],
        }
    )
    tests.extend(winner_tests(stale_labels))
    burn_labels = {"token_type": "total"}
    for identifier, metric in (
        (130, "claude_code_token_usage_tokens_total"),
        (131, "claude_code_cost_usage_USD_total"),
        (132, "codex_turn_token_usage_sum"),
    ):
        tests.append(
            {
                "name": f"burn {identifier} preserves accounts and takes host maximum",
                "interval": "1m",
                "input_series": [
                    series(metric, "0+60x5", **burn_labels),
                    series(metric, "0+120x5", **burn_labels, model="other"),
                    series(metric, "0+300x5", **burn_labels, host_name="fixture-b"),
                    series(metric, "0+120x5", **burn_labels, agent_account="secondary"),
                ],
                "promql_expr_test": [
                    {
                        "expr": PANELS[identifier]["targets"][0]["expr"],
                        "eval_time": "5m",
                        "exp_samples": (
                            [
                                {
                                    "labels": "{}",
                                    "value": 36000 if identifier == 131 else 10,
                                }
                            ]
                            if identifier in {130, 131}
                            else [
                                {"labels": '{agent_account="primary"}', "value": 5},
                                {"labels": '{agent_account="secondary"}', "value": 2},
                            ]
                        ),
                    }
                ],
            }
        )
    check_promtool(tmp_path, {"tests": tests})


def translate(rule: Document) -> Document:
    # Grafana metadata and presentation are intentionally omitted; error/no-data
    # behavior is asserted separately because promtool has no equivalent.
    allowed = {
        "uid",
        "title",
        "condition",
        "for",
        "noDataState",
        "execErrState",
        "labels",
        "annotations",
        "data",
        "isPaused",
    }
    assert not set(rule) - allowed, "unsupported capacity rule keys"
    assert not rule.get("isPaused"), "paused capacity rule"
    assert "keep_firing_for" not in rule
    message = "unsupported capacity rule graph"
    nodes = rule["data"]
    if [n["refId"] for n in nodes] != ["A", "U", "R", "S", "C"] or rule[
        "condition"
    ] != "C":
        raise ValueError(message)
    a, *annotations, c = nodes
    assert all(
        n["datasourceUid"] == "mimir"
        and n["model"]["instant"] is True
        and n["model"]["refId"] == n["refId"]
        and n["model"]["expr"].strip()
        for n in annotations
    )
    if not (
        a["refId"] == a["model"]["refId"] == "A"
        and a["datasourceUid"] == "mimir"
        and a["model"]["instant"] is True
        and not a["model"].get("range")
        and isinstance(a["model"]["expr"], str)
        and a["model"]["expr"].strip()
        and c["refId"] == c["model"]["refId"] == "C"
        and c["datasourceUid"] == "__expr__"
        and c["model"]["type"] == "threshold"
        and c["model"]["expression"] == "A"
        and len(c["model"]["conditions"]) == 1
    ):
        raise ValueError(message)
    condition = c["model"]["conditions"][0]
    evaluator = condition["evaluator"]
    operator = {"gte": ">=", "gt": ">"}.get(evaluator["type"])
    params = evaluator["params"]
    if (
        operator is None
        or len(params) != 1
        or type(params[0]) not in {int, float}
        or not math.isfinite(params[0])
        or set(condition) != {"evaluator"}
    ):
        message = "unsupported threshold"
        raise ValueError(message)
    return {
        "alert": rule["uid"],
        "expr": f"({a['model']['expr']}) {operator} {params[0]}",
        "for": rule["for"],
        "labels": rule["labels"],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "instant",
        "range",
        "empty",
        "condition",
        "reduce",
        "math",
        "expression",
        "compound",
        "evaluator",
    ],
)
def test_capacity_unsupported_graphs(mutation: str) -> None:
    rule = copy.deepcopy(GROUP["rules"][0])
    a, *_, c = rule["data"]
    if mutation == "extra":
        rule["data"].insert(
            1,
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {"refId": "B", "type": "reduce", "expression": "A"},
            },
        )
    elif mutation == "instant":
        a["model"]["instant"] = False
    elif mutation == "range":
        a["model"]["range"] = True
    elif mutation == "empty":
        a["model"]["expr"] = "   "
    elif mutation == "condition":
        rule["condition"] = "B"
    elif mutation in {"reduce", "math"}:
        c["model"]["type"] = mutation
    elif mutation == "expression":
        c["model"]["expression"] = "B"
    elif mutation == "compound":
        c["model"]["conditions"].append({"evaluator": {}})
    else:
        c["model"]["conditions"][0]["evaluator"]["type"] = "outside_range"
    with pytest.raises(ValueError, match=r"unsupported"):
        translate(rule)


def winner_tests(stale_labels: dict[str, str]) -> list[Document]:
    """Fresh readings win before usage is reduced across reporters."""
    tests: list[Document] = []
    winner_labels = (
        '{agent_plan_vendor="openai",agent_plan_window="7d",'
        'agent_account="codex-primary",capacity_key="openai|7d|codex-primary"}'
    )

    def winner_case(
        name: str,
        inputs: list[Document],
        target: int,
        at: str,
        value: float,
    ) -> Document:
        expr = PANELS[110]["targets"][target]["expr"]
        if target == 2:
            expr = "(" + expr + ") != bool (" + expr + ")"  # NaN marks an unknown reset
        return {
            "name": name,
            "interval": "5m" if target == 3 else "1m",
            "input_series": inputs,
            "promql_expr_test": [
                {
                    "expr": expr,
                    "eval_time": at,
                    "exp_samples": [{"labels": winner_labels, "value": value}],
                }
            ],
        }

    tests.append(
        winner_case(
            "a fresh reading beats a carried one from the same host and source",
            [
                series(
                    UTIL,
                    "0.91",
                    **stale_labels,
                    agent_capacity_source="codex_probe",
                    agent_capacity_freshness="carried",
                ),
                series(
                    AGE,
                    "3000",
                    **stale_labels,
                    agent_capacity_source="codex_probe",
                    agent_capacity_freshness="carried",
                ),
                series(
                    UTIL, "0.2", **stale_labels, agent_capacity_source="codex_probe"
                ),
                series(AGE, "20", **stale_labels, agent_capacity_source="codex_probe"),
            ],
            1,
            "0m",
            0.2,
        )
    )
    tests.append(
        winner_case(
            "fresh reporters reduce by utilization across observation times",
            [
                series(
                    UTIL,
                    "0.11 _",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                ),
                series(
                    AGE, "20 _", **stale_labels, agent_capacity_source="codex_rollout"
                ),
                series(
                    UTIL,
                    "_ 0.52",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
                series(
                    AGE,
                    "_ 45",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
            ],
            1,
            "1m",
            0.52,
        )
    )
    tests.append(
        winner_case(
            "a host that stopped reporting does not win on its last age",
            [
                series(
                    UTIL, "0.9 _x9", **stale_labels, agent_capacity_source="codex_probe"
                ),
                series(
                    AGE, "10 _x9", **stale_labels, agent_capacity_source="codex_probe"
                ),
                series(
                    UTIL,
                    "0.3+0x9",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_rollout",
                ),
                series(
                    AGE,
                    "500+0x9",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_rollout",
                ),
            ],
            1,
            "9m",
            0.3,
        )
    )
    tests.append(
        winner_case(
            "the reset countdown comes from the winning source only",
            [
                series(
                    UTIL,
                    "0.11",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    AGE,
                    "3000",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    RESET,
                    "3600",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    UTIL,
                    "0.52",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
                series(
                    AGE,
                    "20",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
            ],
            2,
            "0m",
            1,
        )
    )
    tests.append(
        winner_case(
            "the projection slope comes from the winning source only",
            [
                series(
                    UTIL,
                    "0.1+0.001x290",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    AGE,
                    "3000+0x290",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    RESET,
                    "90000+0x290",
                    **stale_labels,
                    agent_capacity_source="codex_rollout",
                    agent_capacity_freshness="carried",
                ),
                series(
                    UTIL,
                    "0.2+0x290",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
                series(
                    AGE,
                    "20+0x290",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
                series(
                    RESET,
                    "90000+0x290",
                    **stale_labels,
                    host_name="fixture-b",
                    agent_capacity_source="codex_probe",
                ),
            ],
            3,
            "24h",
            0.2,
        )
    )
    return tests


def series(metric: str, values: str, **labels: str) -> Document:
    labels = {
        **KEYS,
        "host_name": "fixture-a",
        "agent_capacity_source": "claude_statusline",
        "agent_capacity_freshness": "fresh",
        **labels,
    }
    return {
        "series": metric
        + "{"
        + ",".join(f"{k}={json.dumps(v)}" for k, v in labels.items())
        + "}",
        "values": values,
    }


@pytest.mark.parametrize("rule", GROUP["rules"], ids=lambda r: r["uid"])
def test_capacity_query_failure_policy(rule: Document) -> None:
    # Promtool cannot execute Grafana's evaluation-error state machine.
    assert (rule["noDataState"], rule["execErrState"]) == ("OK", "OK")


def capacity_reset_path(rule: Document, freshness: str, *, reset: bool) -> Document:
    stale: bool = rule["uid"] == "o11y-capacity-stale"
    labels = {k: v for k, v in KEYS.items() if not stale or k != "agent_plan_window"}
    rendered = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
    edge = "6h" if stale else "15m"
    before = "5h59m59s" if stale else "14m59s"
    inputs = [series(UTIL, "0.9 stale", agent_capacity_freshness=freshness)]
    if reset:
        inputs.append(series(RESET, "3600 stale", agent_capacity_freshness=freshness))
    return {
        "name": f"{rule['uid']} {freshness} reset={reset} expires at {edge}",
        "interval": "1m",
        "input_series": inputs,
        "promql_expr_test": [
            {
                "expr": rule["data"][2]["model"]["expr"],
                "eval_time": time,
                "exp_samples": (
                    [{"labels": rendered, "value": 3600 if reset else -1}]
                    if time != edge
                    else []
                ),
            }
            for time in ("0m", before, edge)
        ],
    }


def test_capacity_reset_paths(tmp_path: Path) -> None:
    check_promtool(
        tmp_path,
        {
            "tests": [
                capacity_reset_path(rule, freshness, reset=reset)
                for rule in GROUP["rules"]
                for freshness in ("fresh", "carried")
                for reset in (True, False)
            ]
        },
    )


def test_capacity_leading_null_history(tmp_path: Path) -> None:
    check_promtool(
        tmp_path,
        {
            "tests": [
                {
                    "name": "history starts empty then returns numeric utilization",
                    "interval": "1m",
                    "input_series": [series(UTIL, "_ 0.25 0.30")],
                    "promql_expr_test": [
                        {
                            "expr": PANELS[140]["targets"][0]["expr"],
                            "eval_time": time,
                            "exp_samples": (
                                [
                                    {
                                        "labels": '{agent_plan_vendor="anthropic",'
                                        'agent_account="primary",agent_plan_window="7d"}',
                                        "value": value,
                                    }
                                ]
                                if value is not None
                                else []
                            ),
                        }
                        for time, value in (("0m", None), ("1m", 0.25), ("2m", 0.30))
                    ],
                }
            ]
        },
    )


@pytest.mark.parametrize(
    ("name", "source", "bucket", "weekly_use", "expected"),
    [
        ("without-bucket", "claude_statusline", "", "0.2", 3600),
        ("agy-bucketed", "agy_statusline", "quota", "0.2", 3600),
        ("tied-windows", "claude_statusline", "", "0.9", 604800),
    ],
)
def test_capacity_stale_reset_window(
    tmp_path: Path, name: str, source: str, bucket: str, weekly_use: str, expected: int
) -> None:
    rule = GROUP["rules"][-1]
    inputs = []
    for window, usage, reset in (("5h", "0.9", "3600"), ("7d", weekly_use, "604800")):
        labels = {"agent_plan_window": window, "agent_capacity_source": source}
        if bucket:
            labels["agent_plan_bucket"] = bucket
        inputs.extend(
            [
                series(UTIL, usage + " stale", **labels),
                series(RESET, reset + " stale", **labels),
            ]
        )
    check_promtool(
        tmp_path,
        {
            "tests": [
                {
                    "name": f"stale reset selects binding window {name}",
                    "interval": "1m",
                    "input_series": inputs,
                    "promql_expr_test": [
                        {
                            "expr": rule["data"][2]["model"]["expr"],
                            "eval_time": time,
                            "exp_samples": [
                                {
                                    "labels": (
                                        '{agent_plan_vendor="anthropic",'
                                        'agent_account="primary"}'
                                    ),
                                    "value": expected,
                                }
                            ],
                        }
                        for time in ("0m", "2h", "5h59m")
                    ],
                }
            ]
        },
    )


def test_capacity_empty_query_recovers_firing_alerts(tmp_path: Path) -> None:
    rules = {r["uid"]: translate(r) for r in GROUP["rules"]}
    exhaustion = rules["o11y-capacity-exhaustion"]
    stale = rules["o11y-capacity-stale"]
    check_promtool(
        tmp_path,
        {
            "tests": [
                {
                    "name": "firing exhaustion clears when query returns nothing",
                    "interval": "5m",
                    "input_series": [
                        series(UTIL, "0+0.003x150 stale"),
                        series(RESET, "604800x150 stale"),
                    ],
                    "alert_rule_test": [
                        assertion(exhaustion, t, firing=t == "12h30m")
                        for t in ("12h30m", "12h45m", "13h")
                    ],
                },
                {
                    "name": "firing stale clears when query returns nothing",
                    "interval": "1m",
                    "input_series": [series(UTIL, "0.9 stale")],
                    "alert_rule_test": [
                        assertion(stale, t, firing=t == "35m")
                        for t in ("35m", "6h", "6h1m")
                    ],
                },
            ],
        },
        rules=list(rules.values()),
    )


def assertion(rule: Document, time: str, *, firing: bool | list[Document]) -> Document:
    labels = firing if isinstance(firing, list) else [{}] if firing else []
    return {
        "eval_time": time,
        "alertname": rule["alert"],
        "exp_alerts": [
            {"exp_labels": {**KEYS, **rule["labels"], **extra}, "exp_annotations": {}}
            for extra in labels
        ],
    }


def test_capacity_rules(tmp_path: Path) -> None:
    """Promtool's alert: is the uid; Grafana's alertname is the title."""
    assert [GROUP[k] for k in ("orgId", "folder", "interval")] == [1, "o11y", "1m"]
    for rule in GROUP["rules"]:
        lookback = {
            "o11y-capacity-exhaustion": 21600,
            "o11y-capacity-stale": 21600,
        }.get(rule["uid"], 900)
        assert rule["data"][0]["relativeTimeRange"] == {"from": lookback, "to": 0}
        assert rule["noDataState"] == "OK"
    rules = [translate(r) for r in GROUP["rules"]]
    assert sorted(r["alert"] for r in rules) == [
        "o11y-capacity-" + k for k in ("exhaustion", "shed", "stale", "stop", "warn")
    ]
    named = {r["alert"].removeprefix("o11y-capacity-"): r for r in rules}
    tests = []

    def case(
        name: str, inputs: list[Document], checks: list[Document], interval: str = "1m"
    ) -> None:
        tests.append(
            {
                "name": name,
                "interval": interval,
                "input_series": inputs,
                "alert_rule_test": checks,
            }
        )

    capacity_rule_cases(rules, named, case)
    case(
        "no capacity series at all, nothing fires",
        [],
        [
            assertion(rule, time, firing=False)
            for time in ("0m", "2m", "10m", "30m", "35m", "40m", "2h", "7d1m")
            for rule in rules
        ],
    )
    case(
        "stopped collector keeps alerting until six-hour history expires",
        [series(UTIL, "0.42 stale")],
        [
            assertion(named["stale"], t, firing=False)
            for t in ("0m", "29m", "30m", "34m")
        ]
        + [
            assertion(named["stale"], f"{minute}m", firing=True)
            for minute in range(35, 121)
        ]
        + [
            assertion(named["stale"], "5h59m", firing=True),
            assertion(named["stale"], "6h", firing=False),
            assertion(named["stale"], "6h1m", firing=False),
        ],
    )
    case(
        "accounts for the same vendor keep stop and stale identities separate",
        [
            series(
                UTIL,
                "0.95x45",
                agent_account="primary",
                agent_capacity_freshness="carried",
            ),
            series(UTIL, "0.25x45", agent_account="secondary"),
            series(
                UTIL,
                "0.20x45",
                host_name="fixture-b",
                agent_account="primary",
                agent_capacity_freshness="carried",
            ),
        ],
        [
            assertion(named["stop"], "2m", firing=[{"agent_account": "primary"}]),
            assertion(named["stale"], "40m", firing=[{"agent_account": "primary"}]),
        ],
    )
    assert len(tests) == 38
    check_promtool(tmp_path, {"tests": tests}, rules=rules)


def tier_edge_cases(tiers: list[Document], case: Callable[..., None]) -> None:
    for rule, edge, severity in zip(
        tiers, (0.70, 0.85, 0.90), ("info", "warning", "critical"), strict=True
    ):
        duration = "2m" if severity == "critical" else "10m"
        pending = "1m" if severity == "critical" else "9m"
        assert rule["for"] == duration
        assert rule["labels"] == {
            "service": "agent-capacity",
            "severity": severity,
            "env": "prd",
        }
        assert rule["expr"].endswith(">= " + str(edge))
        for value in (edge - 0.001, edge, edge + 0.001, 1.0):
            case(
                f"{rule['alert']} edge {value}",
                [series(UTIL, f"{value}+0x15")],
                [
                    assertion(rule, pending, firing=False),
                    assertion(rule, duration, firing=value >= edge),
                    assertion(rule, "11m", firing=value >= edge),
                ],
            )


def exhaustion_slope_cases(exhaustion: Document, case: Callable[..., None]) -> None:
    for direction, values in (
        (-1, "0.9-0.003x288"),
        (0, "0.25+0x288"),
        (1, "0+0.003x288"),
    ):
        case(
            f"slope {direction}",
            [series(UTIL, values), series(RESET, "604800+0x288")],
            [assertion(exhaustion, "24h", firing=direction == 1)],
            "5m",
        )
    for delta in (-3600, -1, 3600):
        case(
            f"12h margin {delta}",
            [
                series(UTIL, "0+0.001x288"),
                series(RESET, f"{343200 + delta}+0x288"),
            ],
            [assertion(exhaustion, "24h", firing=delta > 0)],
            "5m",
        )


def capacity_rule_cases(
    rules: list[Document], named: Document, case: Callable[..., None]
) -> None:
    tiers = [named[k] for k in ("warn", "shed", "stop")]
    tier_edge_cases(tiers, case)
    stale, exhaustion = named["stale"], named["exhaustion"]
    assert (stale["for"], stale["labels"]) == (
        "5m",
        {
            "service": "agent-capacity",
            "severity": "warning",
            "env": "prd",
            "agent_plan_window": "other",
        },
    )
    assert (exhaustion["for"], exhaustion["labels"]) == (
        "30m",
        {"service": "agent-capacity", "severity": "high", "env": "prd"},
    )
    case("no capacity series", [], [assertion(r, "24h", firing=False) for r in rules])
    case(
        "fresh usage suppresses carried usage across hosts",
        [
            series(UTIL, "0.7+0x15"),
            series(
                UTIL,
                "0.9+0x15",
                host_name="fixture-b",
                agent_capacity_source="codex_rollout",
                agent_capacity_freshness="carried",
            ),
        ],
        [
            assertion(r, t, firing=t == "10m" and r is tiers[0])
            for r in tiers
            for t in ("9m", "10m")
        ],
    )
    case(
        "source changes during pending period",
        [
            series(UTIL, "0.9+0x4 stale"),
            series(
                UTIL,
                "_ _ _ _ _ 0.9+0x10",
                agent_capacity_source="agy_statusline",
                agent_capacity_freshness="carried",
            ),
        ],
        [
            assertion(r, t, firing=t == "10m" or r is tiers[2])
            for r in tiers
            for t in ("9m", "10m")
        ],
    )
    case(
        "last fresh zero becomes stale while carried samples continue",
        [
            series(UTIL, "0 stale"),
            series(UTIL, "0x45", agent_capacity_freshness="carried"),
        ],
        [
            assertion(stale, t, firing=t in ("35m", "36m"))
            for t in ("29m", "30m", "34m", "35m", "36m")
        ],
    )
    exhaustion_slope_cases(exhaustion, case)
    source_pin_cases(tiers, stale, case)
    case(
        "missing reset",
        [series(UTIL, "0+0.003472222222222222x80")],
        [assertion(exhaustion, "6h", firing=False)],
        "5m",
    )
    case(
        "model weekly window excluded",
        [
            series(UTIL, "0+0.003472222222222222x80", agent_plan_window="7d_opus"),
            series(RESET, "133200+0x80", agent_plan_window="7d_opus"),
        ],
        [assertion(exhaustion, "6h", firing=False)],
        "5m",
    )
    case(
        "exhaustion pending boundary",
        [series(UTIL, "0+0.003x288"), series(RESET, "604800+0x288")],
        [
            assertion(exhaustion, t, firing=t != "12h24m")
            for t in ("12h24m", "12h25m", "12h26m")
        ],
        "5m",
    )
    capacity_mixed_cases(named, tiers, case)
    case(
        "fresh sample on another window suppresses vendor stale",
        [
            series(UTIL, "0.91x45", agent_capacity_freshness="carried"),
            series(UTIL, "0.20x45", agent_plan_window="5h"),
        ],
        [assertion(stale, "40m", firing=False)],
    )
    case(
        "fresh sample from another host suppresses vendor stale",
        [
            series(UTIL, "0.91x45", agent_capacity_freshness="carried"),
            series(UTIL, "0.20x45", host_name="fixture-b"),
        ],
        [assertion(stale, "40m", firing=False)],
    )
    case(
        "unsupported freshness cannot fire any threshold or stale rule",
        [series(UTIL, "1x45", agent_capacity_freshness="unknown")],
        [assertion(r, "40m", firing=False) for r in [*tiers, stale]],
    )
    case(
        "fresh reading recovers a stale vendor",
        [
            series(UTIL, "0.42 stale _x34 0.42x10"),
            series(UTIL, "0.42x50", agent_capacity_freshness="carried"),
        ],
        [assertion(stale, t, firing=t == "35m") for t in ("34m", "35m", "36m", "40m")],
    )


def source_pin_cases(
    tiers: list[Document], stale: Document, case: Callable[..., None]
) -> None:
    case(
        "series from another exporter are excluded",
        [
            series(UTIL, "0.95x45", agent_capacity_source="statusline"),
            series(UTIL, "0.95x45", agent_capacity_source="manual"),
            series(UTIL, "0.20x45"),
        ],
        [
            assertion(r, t, firing=False)
            for r in [*tiers, stale]
            for t in ("10m", "40m")
        ],
    )
    case(
        "third-party buckets are ordinary windows",
        [
            series(UTIL, "0.72x20", agent_plan_window="3p_5h"),
            series(UTIL, "0.86x20", agent_plan_window="3p_7d"),
        ],
        [
            assertion(
                tiers[0],
                "10m",
                firing=[{"agent_plan_window": "3p_5h"}, {"agent_plan_window": "3p_7d"}],
            ),
            assertion(tiers[1], "10m", firing=[{"agent_plan_window": "3p_7d"}]),
        ],
    )


def capacity_mixed_cases(
    named: Document, tiers: list[Document], case: Callable[..., None]
) -> None:
    stale, exhaustion = named["stale"], named["exhaustion"]
    identities = [
        {"agent_plan_vendor": v, "agent_plan_window": w}
        for v, w in (("anthropic", "5h"), ("openai", "5h"), ("anthropic", "7d_opus"))
    ]
    case(
        "threshold tiers isolate coexisting windows and multi-host max",
        [
            series(UTIL, "0.50x20", agent_plan_vendor="google", agent_plan_window="5h"),
            series(UTIL, "0.72x20", **identities[0]),
            series(UTIL, "0.86x20", **identities[1]),
            series(UTIL, "0.92x20", **identities[2]),
            series(UTIL, "0.88x20", host_name="fixture-b", **identities[2]),
        ],
        [assertion(r, "1m", firing=False) for r in tiers]
        + [assertion(tiers[2], "2m", firing=[identities[2]])]
        + [assertion(r, "10m", firing=identities[i:]) for i, r in enumerate(tiers)],
    )
    inputs = []
    for vendor, window, account, usage, reset in (
        ("anthropic", "7d", "primary", "0.10+0.002x288", "604800x288"),
        ("openai", "7d", "primary", "0.50x288", "604800x288"),
        ("google", "7d", "primary", "0.90-0.002x288", "604800x288"),
        ("anthropic", "7d", "secondary", "0.20+0.0002x288", "100000x288"),
        ("anthropic", "5h", "primary", "0.10+0.002x288", "604800x288"),
    ):
        inputs.extend(
            [
                series(
                    UTIL,
                    usage,
                    agent_plan_vendor=vendor,
                    agent_plan_window=window,
                    agent_account=account,
                ),
                series(
                    RESET,
                    reset,
                    agent_plan_vendor=vendor,
                    agent_plan_window=window,
                    agent_account=account,
                ),
            ]
        )
    case(
        "five-hour window and non-exhausting seven-day slopes "
        "excluded alongside firing weekly window",
        inputs,
        [
            assertion(exhaustion, "10h", firing=False),
            assertion(exhaustion, "24h", firing=True),
        ],
        "5m",
    )
    case(
        "stale vendors with carried emissions and a stopped collector",
        [
            series(UTIL, "0.42 stale"),
            series(UTIL, "0.42x50", agent_capacity_freshness="carried"),
            series(UTIL, "0.42 stale", agent_plan_vendor="openai"),
            series(UTIL, "0.42x50", agent_plan_vendor="google"),
        ],
        [
            assertion(
                stale,
                t,
                firing=[{}, {"agent_plan_vendor": "openai"}] if t == "35m" else [],
            )
            for t in ("29m", "34m", "35m")
        ],
    )

    case(
        "two hosts remain below every tier despite their sum",
        [series(UTIL, "0.50x20"), series(UTIL, "0.50x20", host_name="fixture-b")],
        [assertion(r, t, firing=False) for r in tiers for t in ("10m", "20m")],
    )


def test_translate_rule_keys_and_paused() -> None:
    for key, value in (
        ("futureKey", False),
        ("isPaused", True),
        ("keep_firing_for", "1m"),
    ):
        rule = {**GROUP["rules"][0], key: value}
        with pytest.raises(AssertionError):
            translate(rule)
    rule = GROUP["rules"][0]
    assert translate({**rule, "isPaused": False}) == translate(rule)


def smoke_points(metric: Document) -> dict[tuple[tuple[str, str], ...], list[str]]:
    grouped: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for point in metric["gauge"]["dataPoints"]:
        labels = {a["key"]: a["value"]["stringValue"] for a in point["attributes"]}
        samples = grouped.setdefault(tuple(labels.items()), ["_"] * 1441)
        timestamp = int(point["timeUnixNano"]) // 1000000000
        samples[timestamp // 60] = str(point["asDouble"])
    return grouped


def smoke_inputs() -> list[Document]:
    inputs = []
    for resource in smoke_capacity.histories(86400)["resourceMetrics"]:
        host = resource["resource"]["attributes"][1]["value"]["stringValue"]
        for metric in resource["scopeMetrics"][0]["metrics"]:
            if metric["name"] not in {UTIL, RESET, AGE}:
                continue
            inputs.extend(
                series(
                    metric["name"], " ".join(samples), host_name=host, **dict(identity)
                )
                for identity, samples in smoke_points(metric).items()
            )
    return inputs


def smoke_sample(vendor: str, window: str | None) -> Document:
    labels = {"agent_plan_vendor": vendor, "agent_account": "primary"}
    if window:
        labels["agent_plan_window"] = window
    rendered = ",".join(f"{k}={json.dumps(v)}" for k, v in labels.items())
    return {"labels": "{" + rendered + "}", "value": 1}


def test_capacity_smoke_conditions(tmp_path: Path) -> None:
    """Evaluate the smoke's actual OTLP histories against the live rule queries."""
    expected: list[list[tuple[str, str | None]]] = [
        [("anthropic", "5h"), ("anthropic", "7d")],
        [("anthropic", "7d")],
        [],
        [("google", None)],
    ]
    live_rules = [
        rule for rule in GROUP["rules"] if rule["uid"] != "o11y-capacity-exhaustion"
    ]
    checks = [
        {
            "expr": "(" + translate(rule)["expr"] + ") * 0 + 1",
            "eval_time": "24h",
            "exp_samples": [smoke_sample(v, w) for v, w in identities],
        }
        for rule, identities in zip(live_rules, expected, strict=True)
    ]
    projection = PANELS[120]["targets"][0]["expr"]
    checks.append(
        {
            "expr": (
                "((" + projection + ") != bool (" + projection + ")) and on "
                "(agent_plan_vendor, agent_plan_window, agent_account) max by "
                "(agent_plan_vendor, agent_plan_window, agent_account) (last_over_time("
                + UTIL_FRESH[:-1]
                + ',agent_plan_window="5h"}[15m]))'
            ),
            "eval_time": "24h",
            "exp_samples": [
                smoke_sample("anthropic", "5h"),
                smoke_sample("openai", "5h"),
            ],
        }
    )
    exhaustion = next(
        rule for rule in GROUP["rules"] if rule["uid"] == "o11y-capacity-exhaustion"
    )
    check_promtool(
        tmp_path,
        {
            "tests": [
                {
                    "name": "smoke populated conditions use separate stale vendor",
                    "interval": "1m",
                    "input_series": smoke_inputs(),
                    "promql_expr_test": checks,
                },
                {
                    "name": "data gates accept 144 weekly and 36 five-hour points",
                    "interval": "5m",
                    "input_series": [
                        series(UTIL, "0+0.006x143"),
                        series(RESET, "604800x144"),
                        series(
                            UTIL,
                            "0.25+0x35",
                            agent_plan_vendor="openai",
                            agent_plan_window="5h",
                        ),
                        series(AGE, "0x143"),
                        series(
                            AGE,
                            "0x35",
                            agent_plan_vendor="openai",
                            agent_plan_window="5h",
                        ),
                    ],
                    "promql_expr_test": [
                        {
                            "expr": "(" + translate(exhaustion)["expr"] + ") * 0 + 1",
                            "eval_time": "11h55m",
                            "exp_samples": [smoke_sample("anthropic", "7d")],
                        },
                        {
                            "expr": (
                                "(" + projection + ") and on (agent_plan_vendor, "
                                "agent_plan_window, agent_account) max by "
                                "(agent_plan_vendor, agent_plan_window, agent_account) "
                                "(last_over_time("
                                + UTIL_FRESH[:-1]
                                + ',agent_plan_window="5h"}[15m]))'
                            ),
                            "eval_time": "2h55m",
                            "exp_samples": [
                                {
                                    "labels": (
                                        '{agent_plan_vendor="openai",'
                                        'agent_plan_window="5h",'
                                        'agent_account="primary"}'
                                    ),
                                    "value": smoke_capacity.OPENAI_USAGE,
                                }
                            ],
                        },
                    ],
                },
            ],
        },
    )


def prefer_fresh_checks(bucket: str) -> list[Document]:
    window_labels = (
        '{agent_plan_vendor="openai",agent_plan_window="7d",'
        'agent_account="codex-primary"'
    )
    checks = [
        {
            "expr": target["expr"],
            "eval_time": "24h",
            "exp_samples": [
                {
                    "labels": window_labels
                    + ',capacity_key="openai|7d|codex-primary"}',
                    "value": value,
                }
            ],
        }
        for target, value in zip(
            PANELS[110]["targets"], (0.82, 0.82, 86400, 0.82), strict=True
        )
    ]
    checks.extend(
        {
            "expr": PANELS[panel]["targets"][0]["expr"],
            "eval_time": "24h",
            "exp_samples": [
                {
                    "labels": window_labels
                    + (
                        f',agent_plan_bucket="{bucket}"'
                        if bucket and panel == 140
                        else ""
                    )
                    + "}",
                    "value": 0.82,
                }
            ],
        }
        for panel in (120, 140)
    )
    checks.extend(
        {
            "expr": rule["data"][1]["model"]["expr"],
            "eval_time": "24h",
            "exp_samples": [
                {
                    "labels": (
                        '{agent_plan_vendor="openai",agent_account="codex-primary"}'
                    )
                    if rule["uid"].endswith("stale")
                    else window_labels + "}",
                    "value": 0.82,
                }
            ],
        }
        for rule in GROUP["rules"]
    )
    return checks


@pytest.mark.parametrize("bucket", ["", "weekly"])
def test_capacity_prefer_fresh(tmp_path: Path, bucket: str) -> None:
    rules = [translate(rule) for rule in GROUP["rules"]]
    tests = []
    for carried in (0.53, 0.95):
        labels = {
            "agent_plan_vendor": "openai",
            "agent_plan_window": "7d",
            "agent_account": "codex-primary",
        }
        if bucket:
            labels["agent_plan_bucket"] = bucket
        inputs = []
        for freshness, source, value, age in (
            ("fresh", "codex_probe", 0.82, 300),
            ("carried", "codex_rollout", carried, 0),
        ):
            for metric, reading in ((UTIL, value), (AGE, age), (RESET, 172800)):
                inputs.append(
                    series(
                        metric,
                        f"{reading}x1440",
                        **labels,
                        agent_capacity_source=source,
                        agent_capacity_freshness=freshness,
                        host_name="fixture-a" if freshness == "fresh" else "fixture-b",
                    )
                )
        tests.append(
            {
                "name": f"fresh 0.82 beats newer carried {carried}",
                "interval": "1m",
                "input_series": inputs,
                "promql_expr_test": prefer_fresh_checks(bucket),
                "alert_rule_test": [
                    assertion(
                        rule,
                        "24h",
                        firing=[
                            {
                                k: v
                                for k, v in labels.items()
                                if k != "agent_plan_bucket"
                            }
                        ]
                        if rule["alert"].endswith("warn")
                        else False,
                    )
                    for rule in rules
                ],
            }
        )
    check_promtool(tmp_path, {"tests": tests}, rules=rules)
