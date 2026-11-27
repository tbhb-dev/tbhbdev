"""Run the opt-in task flow with simulated DSM actions and no host connections."""

# cspell:words synogroup memberadd

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol, TypedDict, cast

import pytest
import yaml

ROOT = next(
    path for path in Path(__file__).resolve().parents if (path / "ansible").is_dir()
)
TASKS = ROOT / "ansible/roles/dsm_accounts/tasks"
TAG = "dsm_accounts_agent_log_group"


class DsmOptions(TypedDict, total=False):
    members: list[str]
    events: list[str]
    group_rc: int
    group_output: str
    help: str
    stat: dict[str, str | int | bool]
    login_rc: int
    add_rc: int
    ignore_add: bool
    lose_member: bool
    proof_rc: int


class DsmState(TypedDict):
    members: list[str]
    events: list[str]


class DsmRun(Protocol):
    def __call__(
        self, state: DsmOptions, *options: str
    ) -> tuple[subprocess.CompletedProcess[str], DsmState]: ...


# Only the remote actions are simulated. Ansible itself evaluates imports,
# check mode, when, changed_when, delegation, variables, and assertions.
ACTION = """
import json
from pathlib import Path
from ansible.plugins.action import ActionBase

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        path = Path(task_vars["dsm_fixture_state"])
        state = json.loads(path.read_text())
        kind = self._task.args["kind"]
        args = self._task.args["params"]
        result = {"rc": 0, "stdout": "", "stderr": "", "changed": False}
        if kind == "command":
            assert self._task.become is True
            argv = args["argv"]
            assert argv in (["synogroup", "--get", "log"],
                            ["synogroup", "--help"],
                            ["synogroup", "--memberadd", "log", "agent"])
            event = argv[1]
            if event == "--get":
                members = "".join(f"{i}:[{name}]\\n"
                                  for i, name in enumerate(state["members"]))
                result["stdout"] = state.get("group_output",
                    "Group Name: [log]\\nGroup Type: [AUTH_LOCAL]\\n"
                    "Group ID:   [19]\\nGroup Members: \\n" + members)
                result["rc"] = state.get("group_rc", 0)
            elif event == "--help":
                result["stderr"] = state.get("help",
                                             "    --memberadd groupname username")
                result["rc"] = 1  # Usage output need not exit successfully.
            else:
                assert not self._task.check_mode
                assert "agent" not in state["members"]
                result["rc"] = state.get("add_rc", 0)
                if result["rc"] == 0 and not state.get("ignore_add"):
                    state["members"].append("agent")
                if state.get("lose_member"):
                    state["members"].remove("existing")
        elif kind == "stat":
            event = "stat"
            assert self._task.become is True
            assert args == {"path": "/var/log/auth.log", "follow": False,
                            "get_checksum": False, "get_mime": False,
                            "get_attributes": False}
            result["stat"] = {"exists": True, "isreg": True, "islnk": False,
                              "uid": 0, "gid": 19, "gr_name": "log",
                              "mode": "0640"} | state.get("stat", {})
        else:
            assert kind == "raw"
            assert self._task.become is False
            assert task_vars["ansible_user"] == "agent"
            assert self._play_context.remote_user == "agent"
            assert task_vars["ansible_become_password"] == ""
            assert "ControlMaster=no" in task_vars["ansible_ssh_args"]
            assert "ControlPath=none" in task_vars["ansible_ssh_args"]
            assert "IdentityAgent=none" in task_vars["ansible_ssh_extra_args"]
            assert "BatchMode=yes" in task_vars["ansible_ssh_extra_args"]
            assert "agent-key" in self._templar.template(
                task_vars["ansible_ssh_extra_args"])
            assert self._task.no_log is True
            assert self._task.check_mode is False
            assert "whoami" in args["_raw_params"]
            event = ("proof" if "/var/log/auth.log" in args["_raw_params"]
                     else "login")
            result["rc"] = state.get(event + "_rc",
                int("agent" not in state["members"]) if event == "proof" else 0)
            result["stdout"] = "AUTH_CONTENT_MUST_NOT_ESCAPE"
            result["stderr"] = "AUTH_CONTENT_MUST_NOT_ESCAPE"
        state["events"].append(event)
        path.write_text(json.dumps(state))
        return result
"""


@pytest.fixture
def run_dsm(tmp_path: Path) -> DsmRun:
    actions = tmp_path / "actions"
    actions.mkdir()
    (actions / "dsm_fixture.py").write_text(ACTION)
    for filename in ("agent_log_group.yml", "agent_log_group_read.yml"):
        tasks = yaml.safe_load((TASKS / filename).read_text())
        for task in tasks:
            for kind in ("command", "stat", "raw"):
                module = "ansible.builtin." + kind
                if module in task:
                    params = task.pop(module)
                    if kind == "raw":
                        params = {"_raw_params": params}
                    task["dsm_fixture"] = {"kind": kind, "params": params}
        (tmp_path / filename).write_text(yaml.safe_dump(tasks))
    config = tmp_path / "ansible.cfg"
    config.write_text(
        f"[defaults]\naction_plugins={actions}\n"
        f"local_tmp={tmp_path / 'local'}\nretry_files_enabled=False\n"
    )
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {
                        "ansible_user": "ops",
                        "ansible_become_password": "fixture-unused-password",
                        "dsm_accounts_agent_log_private_key_file": "/fixture/agent-key",
                        "dsm_fixture_state": str(tmp_path / "state.json"),
                    },
                    "tasks": [
                        {
                            "ansible.builtin.import_tasks": "agent_log_group.yml",
                            "tags": ["never", TAG],
                        }
                    ],
                }
            ]
        )
    )

    def run(
        state: DsmOptions, *options: str
    ) -> tuple[subprocess.CompletedProcess[str], DsmState]:
        state = {"members": ["existing", "service"], "events": []} | state
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(state))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ansible.cli.playbook",
                "-i",
                "localhost,",
                str(playbook),
                *options,
            ],
            env=os.environ | {"ANSIBLE_CONFIG": str(config), "ANSIBLE_NOCOLOR": "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert "AUTH_CONTENT_MUST_NOT_ESCAPE" not in result.stdout + result.stderr
        return result, cast("DsmState", json.loads(state_path.read_text()))

    return run


def test_tag_is_opt_in_in_the_real_role() -> None:
    tasks = yaml.safe_load((TASKS / "main.yml").read_text())
    tagged = [task for task in tasks if TAG in task.get("tags", [])]
    assert len(tagged) == 1
    assert tagged[0]["tags"] == ["never", TAG]
    assert tagged[0]["ansible.builtin.import_tasks"] == "agent_log_group.yml"


def test_default_run_skips_every_action(run_dsm: DsmRun) -> None:
    result, state = run_dsm({})
    assert result.returncode == 0, result.stdout + result.stderr
    assert state["events"] == []


def test_a_missing_agent_identity_stops_before_remote_reads(run_dsm: DsmRun) -> None:
    result, state = run_dsm(
        {}, "--tags", TAG, "-e", "dsm_accounts_agent_log_private_key_file="
    )
    assert result.returncode != 0
    assert state["events"] == []
    assert "existing key authorized for agent" in result.stdout


def test_apply_preserves_members_and_second_apply_is_unchanged(run_dsm: DsmRun) -> None:
    result, state = run_dsm({}, "--tags", TAG)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "changed=1" in result.stdout
    assert state["members"] == ["existing", "service", "agent"]
    assert state["events"] == [
        "--get",
        "stat",
        "--help",
        "login",
        "--memberadd",
        "--get",
        "stat",
        "proof",
    ]
    result, state = run_dsm({"members": state["members"]}, "--tags", TAG)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "changed=0" in result.stdout
    assert "--memberadd" not in state["events"]
    assert state["events"].count("--get") == 2
    assert state["events"][-1] == "proof"


@pytest.mark.parametrize("members", [["existing"], ["existing", "agent"]])
def test_check_reads_real_state_and_never_writes(
    run_dsm: DsmRun, members: list[str]
) -> None:
    result, state = run_dsm({"members": members}, "--tags", TAG, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"changed={int('agent' not in members)}" in result.stdout
    assert state["members"] == members
    assert state["events"] == [
        "--get",
        "stat",
        "--help",
        "login",
        "--get",
        "stat",
        "proof",
    ]


@pytest.mark.parametrize(
    "state",
    [
        {"group_rc": 1},
        {"group_output": "Group Name: [other]\nGroup ID: [19]\nGroup Members:\n"},
        {"group_output": "Group Name: [log]\nGroup Members:\n"},
        {"group_output": "Group Name: [log]\nGroup ID: [19]\n"},
        {"group_output": "Group Name: [log]\nGroup ID: [19]\nGroup Members:\nunknown"},
        {"stat": {"exists": False}},
        {"stat": {"isreg": False}},
        {"stat": {"islnk": True}},
        {"stat": {"uid": 1}},
        {"stat": {"gr_name": "other"}},
        {"stat": {"gid": 20}},
        {"stat": {"mode": "0660"}},
        {"help": "    --member groupname username"},
        {"login_rc": 2},
    ],
)
def test_unsafe_preconditions_fail_before_any_write(
    run_dsm: DsmRun, state: DsmOptions
) -> None:
    result, after = run_dsm(state, "--tags", TAG, "--check")
    assert result.returncode != 0
    assert after["members"] == ["existing", "service"]
    assert "--memberadd" not in after["events"]


@pytest.mark.parametrize(
    "state",
    [{"ignore_add": True}, {"lose_member": True}, {"add_rc": 1}, {"proof_rc": 1}],
)
def test_apply_fails_on_write_read_back_or_read_proof_failure(
    run_dsm: DsmRun, state: DsmOptions
) -> None:
    result, after = run_dsm(state, "--tags", TAG)
    assert result.returncode != 0
    assert "--memberadd" in after["events"]


@pytest.mark.parametrize(
    "state", [{"members": ["agent"], "proof_rc": 1}, {"proof_rc": 2}]
)
def test_check_does_not_hide_unexpected_proof_failures(
    run_dsm: DsmRun, state: DsmOptions
) -> None:
    result, after = run_dsm(state, "--tags", TAG, "--check")
    assert result.returncode != 0
    assert "--memberadd" not in after["events"]


@pytest.mark.parametrize("identity", ["agent", "ops"])
def test_proof_shell_opens_without_printing_content(
    tmp_path: Path, identity: str
) -> None:
    log = tmp_path / "auth.log"
    log.write_text("AUTH_CONTENT_MUST_NOT_ESCAPE\n")
    tasks = yaml.safe_load((TASKS / "agent_log_group.yml").read_text())
    (proof,) = [
        task["ansible.builtin.raw"]
        for task in tasks
        if "register" in task and task["register"] == "dsm_accounts_agent_log_proof"
    ]
    script = proof.replace("/var/log/auth.log", str(log)).replace(
        "whoami", f"printf {identity}"
    )
    result = subprocess.run(["sh", "-c", script], capture_output=True, check=False)
    assert result.returncode == (0 if identity == "agent" else 2)
    assert result.stdout == result.stderr == b""
    log.unlink()
    result = subprocess.run(["sh", "-c", script], capture_output=True, check=False)
    assert result.returncode == (1 if identity == "agent" else 2)
    assert result.stdout == result.stderr == b""
