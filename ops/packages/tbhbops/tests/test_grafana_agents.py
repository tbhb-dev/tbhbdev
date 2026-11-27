"""Exercise Grafana login reconciliation with a synthetic admin API."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = next(p for p in Path(__file__).parents if (p / "o11y/compose.yaml").exists())


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX shell")
@pytest.mark.parametrize("exists", [False, True])
def test_agents_creation_and_password_rotation(tmp_path: Path, exists: bool) -> None:
    admin = "SyntheticAdmin42"
    password = "SyntheticAgents84"  # noqa: S105 - synthetic API fixture
    (tmp_path / "grafana_admin_password").write_text(admin)
    (tmp_path / "grafana_agents_password").write_text(password)
    curl = tmp_path / "curl"
    curl.with_suffix(".py").write_text(
        """import json, os, pathlib, sys
args = sys.argv[1:]
config = sys.stdin.read()
assert 'SyntheticAdmin42' not in ' '.join(args)
assert 'SyntheticAgents84' not in ' '.join(args)
assert 'user = "admin:SyntheticAdmin42"' in config
url = args[-1]
body = next(
(s[8:-1].replace('\\\\"', '"')
 for s in config.splitlines() if s.startswith('data = "')), '{}',
)
body = json.loads(body)
method = args[args.index('-X') + 1] if '-X' in args else 'GET'
with open(os.environ['CALLS'], 'a') as log:
    log.write(json.dumps([method, url, body]) + '\\n')
reply = '{"id":7}' if 'loginOrEmail=agents' in args else '{"id":2}'
pathlib.Path(args[args.index('-o') + 1]).write_text(reply)
print('412' if method == 'POST' and os.environ['EXISTS'] == 'yes' else '200', end='')
"""
    )
    curl.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$0.py" "$@"\n')
    curl.chmod(0o700)
    script = (
        (ROOT / "o11y/config/grafana/owner.sh")
        .read_text()
        .replace("/run/o11y/", str(tmp_path) + "/")
    )
    calls = tmp_path / "calls"
    result = subprocess.run(  # nosec B603 B607: fixed shell and synthetic API
        ["/bin/sh", "-c", script],
        capture_output=True,
        check=False,
        env=os.environ
        | {
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "O11Y_GRAFANA_OWNER_LOGIN": "owner@example.invalid",
            "CALLS": str(calls),
            "EXISTS": "yes" if exists else "no",
        },
    )
    assert result.returncode == 0, result.stderr
    assert admin.encode() not in result.stdout + result.stderr
    assert password.encode() not in result.stdout + result.stderr
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert requests[2] == [
        "PATCH",
        "http://grafana:3000/api/org/users/2",
        {"role": "Admin"},
    ]
    assert requests[4][2] == {"login": "agents", "name": "agents", "password": password}
    assert requests[-1] == [
        "PATCH",
        "http://grafana:3000/api/org/users/7",
        {"role": "Editor"},
    ]
    assert [r for r in requests if r[0] == "PUT"] == (
        [
            [
                "PUT",
                "http://grafana:3000/api/admin/users/7/password",
                {"password": password},
            ]
        ]
        if exists
        else []
    )


def test_agents_secret_delivery_and_mounts() -> None:
    role = ROOT / "ansible/roles/o11y"
    tasks = yaml.safe_load((role / "tasks/release.yml").read_text())
    ensure, delivery = tasks[-3:-1]
    assert "--generate-password=letters,digits,32" in ensure["ansible.builtin.shell"]
    for task in (ensure, delivery):
        assert task["no_log"] is True
        assert task["diff"] is False
        assert "register" not in task
    template = delivery["ansible.builtin.template"]
    assert template["owner"] == template["group"] == "472"
    assert template["mode"] == "0400"
    assert template["dest"] == "{{ o11y_data_dir }}/secrets/grafana_agents_password"
    delivery_template = (role / "templates" / template["src"]).read_text()
    assert "o11y_grafana_agents_password_ref | quote" in delivery_template
    assert "o11y_op_account | quote" in delivery_template
    services = yaml.safe_load((ROOT / "o11y/compose.yaml").read_text())["services"]
    for service in ("grafana", "grafana-owner"):
        assert (
            "${O11Y_DATA_DIR:?}/secrets/grafana_agents_password:"
            "/run/o11y/grafana_agents_password:ro"
        ) in services[service]["volumes"]
