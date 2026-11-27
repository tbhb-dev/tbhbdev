from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_promtool_exercises_the_provisioned_rule() -> None:
    provisioned = (
        ROOT / "o11y/config/grafana/provisioning/alerting/rules.yaml"
    ).read_text()
    group = next(
        group
        for group in yaml.safe_load(provisioned)["groups"]
        if group["name"] == "ca-backup"
    )
    rule = group["rules"][0]
    fixture = yaml.safe_load(
        (Path(__file__).parent / "promtool/rules.yaml").read_text()
    )
    tested = fixture["groups"][0]["rules"][0]
    query, condition = rule["data"]
    evaluator = condition["model"]["conditions"][0]["evaluator"]
    threshold = evaluator["params"][0]
    assert rule["condition"] == condition["refId"]
    assert condition["model"]["expression"] == query["refId"]
    assert rule["for"] == "0s"
    assert evaluator["type"] == "gt"
    assert tested["expr"] == f"({query['model']['expr']}) > {threshold}"
    assert tested["labels"] == rule["labels"]
