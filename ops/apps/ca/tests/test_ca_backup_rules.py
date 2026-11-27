from pathlib import Path

import yaml

RULES = (
    Path(__file__).parents[3] / "o11y/config/grafana/provisioning/alerting/rules.yaml"
)
FIXTURE = Path(__file__).parent / "promtool/ca-backup.yaml"


def test_provisioned_ca_backup_alert() -> None:
    provisioned = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    group = next(
        group for group in provisioned["groups"] if group["name"] == "ca-backup"
    )
    rule = next(rule for rule in group["rules"] if rule["uid"] == "ca-backup-unhealthy")
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    failure = fixture["tests"][0]["alert_rule_test"][1]
    labels = failure["exp_alerts"][0]["exp_labels"]
    host = labels["host"]

    query, condition = rule["data"]
    model = condition["model"]
    assert rule["condition"] == condition["refId"]
    assert model["expression"] == query["refId"]
    assert model["type"] == "threshold"
    assert model["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}
    assert query["model"]["expr"] == (
        f'((max(last_over_time(ca_backup_last_run_failed{{host="{host}"}}[26h]))'
        " or vector(0)) > bool 0)"
        " + (time() - (max(last_over_time(ca_backup_last_success_or_start_timestamp_seconds{"
        f' host="{host}"}}[26h])) or vector(0)) >= bool 93600)'
    )
    assert rule["for"] == "0s"
    assert rule["labels"] == labels
