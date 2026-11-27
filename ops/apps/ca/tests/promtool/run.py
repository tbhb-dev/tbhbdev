"""Run the CA backup alert fixtures against the provisioned rule.

Run with Python, PyYAML, and promtool available.
"""

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

FIXTURES = Path(__file__).resolve().parent
RULES = FIXTURES.parents[3] / "o11y/config/grafana/provisioning/alerting/rules.yaml"


def main() -> int:
    provisioned = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    group = next(
        group for group in provisioned["groups"] if group["name"] == "ca-backup"
    )
    rule = next(rule for rule in group["rules"] if rule["uid"] == "ca-backup-unhealthy")
    models = {entry["refId"]: entry["model"] for entry in rule["data"]}
    condition = models[rule["condition"]]
    evaluator = condition["conditions"][0]["evaluator"]
    if evaluator["type"] != "gt":
        raise ValueError("Expected a greater-than threshold for the CA backup alert")
    query = models[condition["expression"]]["expr"]
    prom_rule = {
        "alert": "CaBackupUnhealthy",
        "expr": f"({query}) > {evaluator['params'][0]}",
        "for": rule["for"],
        "labels": rule["labels"],
    }
    with TemporaryDirectory() as directory:
        staging = Path(directory)
        (staging / "rules.yaml").write_text(
            yaml.safe_dump(
                {
                    "groups": [
                        {
                            "name": group["name"],
                            "interval": group["interval"],
                            "rules": [prom_rule],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        shutil.copyfile(FIXTURES / "ca-backup.yaml", staging / "ca-backup.yaml")
        return subprocess.run(
            ["promtool", "test", "rules", "ca-backup.yaml"],
            cwd=staging,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
