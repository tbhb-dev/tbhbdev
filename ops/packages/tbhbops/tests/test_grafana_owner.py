"""Exercise role reconciliation with a local curl stand-in."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).parents if (p / "o11y").is_dir())

CURL = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
config = sys.stdin.read()
assert config.startswith('user = "admin:test-password"\n')
state = Path(os.environ["STATE"])
users = json.loads(state.read_text())
url = args[-1]
method = args[args.index("-X") + 1] if "-X" in args else "GET"
body = {}
status = "200"
if url.endswith("/api/users/lookup"):
    body = {"id": 10 if "loginOrEmail=agents" in args else 1}
elif url.endswith("/api/org/users"):
    body = users
elif "/api/users/" in url:
    user_id = int(url.rsplit("/", 1)[1])
    body = {"isGrafanaAdmin": user_id == 7}
    if user_id == 8:
        status = "500"
    if user_id == 9:
        sys.exit(7)
elif method == "PATCH":
    user_id = int(url.rsplit("/", 1)[1])
    role = "Admin" if "Admin" in config else "Editor"
    with Path(os.environ["CALLS"]).open("a") as log:
        log.write(f"{user_id} {role}\n")
    for user in users:
        if user["userId"] == user_id:
            user["role"] = role
    state.write_text(json.dumps(users))
Path(args[args.index("-o") + 1]).write_text(
    json.dumps(body, separators=(",", ":"),
               indent=int(os.environ["INDENT"]) if isinstance(body, list) else None)
)
print(status, end="")
"""


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX sh")
def test_promotes_only_other_viewers_and_is_idempotent(
    tmp_path: Path, indent: int | None
) -> None:
    users = [
        {"userId": 1, "login": "owner@example.invalid", "role": "Viewer"},
        {"userId": 2, "login": "viewer@example.invalid", "role": "Viewer"},
        {"userId": 3, "login": "editor@example.invalid", "role": "Editor"},
        {"userId": 4, "login": "admin@example.invalid", "role": "Admin"},
        {"userId": 5, "login": "second@example.invalid", "role": "Viewer"},
        # Even if the list still reports the owner as Viewer, exclude the login.
        {"userId": 6, "login": "owner@example.invalid", "role": "Viewer"},
        {"userId": 7, "login": "server-admin@example.invalid", "role": "Viewer"},
        {"userId": 8, "login": "failed-lookup@example.invalid", "role": "Viewer"},
        {"userId": 9, "login": "curl-failure@example.invalid", "role": "Viewer"},
    ]
    state, calls = tmp_path / "users.json", tmp_path / "calls"
    state.write_text(json.dumps(users))
    curl = tmp_path / "curl"
    curl.write_text(f"#!{sys.executable}\n{CURL}")
    curl.chmod(0o700)
    password = tmp_path / "password"
    password.write_text("test-password\n")
    script = (
        (ROOT / "o11y/config/grafana/owner.sh")
        .read_text()
        .replace("/run/o11y/grafana_admin_password", str(password))
        .replace("/run/o11y/grafana_agents_password", str(password))
    )
    env = os.environ | {
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "O11Y_GRAFANA_OWNER_LOGIN": "owner@example.invalid",
        "STATE": str(state),
        "CALLS": str(calls),
        "INDENT": str(indent or 0),
    }
    for _ in range(2):
        result = subprocess.run(
            ["sh", "-c", script], env=env, capture_output=True, text=True, check=True
        )
        assert "test-password" not in result.stdout + result.stderr
        assert "user lookup failed for id 8; skipping" in result.stderr
        assert "user lookup failed for id 9; skipping" in result.stderr
    assert calls.read_text().splitlines() == [
        "1 Admin",
        "2 Editor",
        "5 Editor",
        "10 Editor",
        "1 Admin",
        "10 Editor",
    ]
    assert [user["role"] for user in json.loads(state.read_text())] == [
        "Admin",
        "Editor",
        "Editor",
        "Admin",
        "Editor",
        "Viewer",
        "Viewer",
        "Viewer",
        "Viewer",
    ]
