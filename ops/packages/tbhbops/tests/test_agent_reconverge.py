"""Render the reconverge schedules without contacting or changing any host."""

# cspell:ignore execstart successexitstatus onactivesec onunitactivesec wantedby

import configparser
import plistlib
from pathlib import Path

import jinja2
import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = next(path for path in Path(__file__).parents if (path / "ansible").is_dir())
ROLE = ROOT / "ansible/roles/agent_reconverge"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))

MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))

LINUX = yaml.safe_load((ROLE / "tasks/linux.yml").read_text(encoding="utf-8"))

MACOS = yaml.safe_load((ROLE / "tasks/macos.yml").read_text(encoding="utf-8"))

SNAPSHOT = yaml.safe_load(
    (ROOT / "ansible/roles/mac_snapshot_thin/tasks/main.yml").read_text(
        encoding="utf-8"
    )
)
LAUNCH_DAEMON = yaml.safe_load(
    (ROOT / "ansible/tasks/launch-daemon.yml").read_text(encoding="utf-8")
)

HANDLERS = yaml.safe_load((ROLE / "handlers/main.yml").read_text(encoding="utf-8"))


def render(name: str, **variables: object) -> str:
    environment = jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True
    )
    template = (ROLE / "templates" / name).read_text(encoding="utf-8")
    return environment.from_string(template).render(DEFAULTS | variables)


def conditions(expressions: list[str], **variables: object) -> bool:
    templar = Templar(loader=DataLoader(), variables=DEFAULTS | variables)
    return all(
        templar.evaluate_conditional(trust_as_template(expression))
        for expression in expressions
    )


@pytest.mark.parametrize(
    ("account", "clone", "interval"),
    [
        ("agent", "Code/github.com/example-org/account-config", 900),
        ("builder", "checkouts/agent-config", 1800),
    ],
)
def test_linux_units(account: str, clone: str, interval: int) -> None:
    variables = {
        "agent_reconverge_account": account,
        "agent_reconverge_clone": clone,
        "agent_reconverge_interval_seconds": interval,
    }
    service = configparser.ConfigParser(interpolation=None)
    service.read_string(render("reconverge-agent.service.j2", **variables))
    assert dict(service["Service"]) == {
        "type": "oneshot",
        "execstart": f'"%h/{clone}/tony/reconverge.sh" --account {account}',
        "successexitstatus": "0 1",
    }
    timer = configparser.ConfigParser()
    timer.read_string(render("reconverge-agent.timer.j2", **variables))
    assert dict(timer["Timer"]) == {
        "onactivesec": "1min",
        "onunitactivesec": f"{interval}s",
        "persistent": "true",
    }
    assert dict(timer["Install"]) == {"wantedby": "timers.target"}


@pytest.mark.parametrize(
    ("account", "home", "clone", "interval"),
    [
        (
            "agent",
            "/Users/example-user",
            "Code/github.com/example-org/account-config",
            900,
        ),
        ("builder", "/Volumes/Agent & Build/builder", "checkouts/config", 1800),
    ],
)
def test_macos_plist(account: str, home: str, clone: str, interval: int) -> None:
    text = render(
        "dev.tbhb.reconverge.agent.plist.j2",
        agent_reconverge_account=account,
        agent_reconverge_identity={"home": home},
        agent_reconverge_clone=clone,
        agent_reconverge_interval_seconds=interval,
    )
    assert plistlib.loads(text.encode()) == {
        "Label": "dev.tbhb.reconverge.agent",
        "UserName": account,
        "WorkingDirectory": home,
        "ProgramArguments": [
            "/bin/bash",
            "-c",
            f'exec "$HOME/{clone}/tony/reconverge.sh" --account {account}',
        ],
        "EnvironmentVariables": {
            "HOME": home,
            "PATH": "/Library/tbhb-python/bin:/opt/homebrew/bin:/usr/bin:/bin",
        },
        "StartInterval": interval,
        "RunAtLoad": True,
    }


@pytest.mark.parametrize("playbook", ["agent-accounts", "lab-hosts", "lab-macos"])
def test_playbook_order(playbook: str) -> None:
    plays = yaml.safe_load(
        (ROOT / f"ansible/playbooks/{playbook}.yml").read_text(encoding="utf-8")
    )
    roles = plays[0]["roles"]
    assert roles[roles.index("agent_account") + 1] == "agent_reconverge"


def test_linux_installation_contract() -> None:
    linux = LINUX
    units = linux[1]
    assert units["loop"] == ["reconverge-agent.service", "reconverge-agent.timer"]
    assert units["ansible.builtin.template"] == {
        "src": "{{ item }}.j2",
        "dest": "{{ agent_reconverge_identity.home }}/.config/systemd/user/{{ item }}",
        "owner": "{{ agent_reconverge_account }}",
        "group": "{{ agent_reconverge_identity.gid }}",
        "mode": "0644",
    }
    assert linux[2]["ansible.builtin.command"] == {
        "argv": ["loginctl", "enable-linger", "{{ agent_reconverge_account }}"],
        "creates": "/var/lib/systemd/linger/{{ agent_reconverge_account }}",
    }
    assert linux[3]["ansible.builtin.systemd_service"] == {
        "name": "user@{{ agent_reconverge_identity.uid }}.service",
        "state": "started",
    }
    timer = linux[-1]
    handler = HANDLERS[0]
    for task in (timer, handler):
        assert task["become"] is True
        assert task["become_user"] == "{{ agent_reconverge_account }}"
        assert task["environment"] == {
            "XDG_RUNTIME_DIR": "/run/user/{{ agent_reconverge_identity.uid }}"
        }
        assert task["when"] == "not ansible_check_mode"
    assert timer["ansible.builtin.systemd_service"] == {
        "name": "reconverge-agent.timer",
        "scope": "user",
        "enabled": True,
        "state": "started",
    }
    assert handler["ansible.builtin.systemd_service"]["daemon_reload"] is True
    assert handler["ansible.builtin.systemd_service"]["state"] == "restarted"
    assert units["notify"] == handler["name"]


def test_macos_installation_contract() -> None:
    for caller, label, src in (
        (
            MACOS[-1],
            "dev.tbhb.reconverge.agent",
            "templates/dev.tbhb.reconverge.agent.plist.j2",
        ),
        (
            SNAPSHOT[-1],
            "dev.tbhb.mac-snapshot-thin",
            "files/dev.tbhb.mac-snapshot-thin.plist",
        ),
    ):
        assert caller["ansible.builtin.include_tasks"] == (
            "{{ role_path }}/../../tasks/launch-daemon.yml"
        )
        assert caller["vars"] == {
            "launch_daemon_label": label,
            "launch_daemon_source": "{{ role_path }}/" + src,
        }
    state, plist, bootout, bootstrap = LAUNCH_DAEMON
    assert plist["ansible.builtin.template"] == {
        "src": "{{ launch_daemon_source }}",
        "dest": "/Library/LaunchDaemons/{{ launch_daemon_label }}.plist",
        "owner": "root",
        "group": "wheel",
        "mode": "0644",
    }
    assert state["check_mode"] is False
    assert state["changed_when"] is False
    assert bootout["ansible.builtin.command"]["argv"] == [
        "/bin/launchctl",
        "bootout",
        "system/{{ launch_daemon_label }}",
    ]
    assert bootstrap["ansible.builtin.command"]["argv"] == [
        "/bin/launchctl",
        "bootstrap",
        "system",
        "/Library/LaunchDaemons/{{ launch_daemon_label }}.plist",
    ]


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("loaded", [False, True])
def test_launchd_change_and_check_guards(
    *, check: bool, changed: bool, loaded: bool
) -> None:
    variables = {
        "ansible_check_mode": check,
        "launch_daemon_plist": {"changed": changed},
        "launch_daemon_status": {"rc": 0 if loaded else 113},
    }
    assert conditions(LAUNCH_DAEMON[-1]["when"], **variables) is (
        not check and (changed or not loaded)
    )
    assert conditions(LAUNCH_DAEMON[-2]["when"], **variables) is (
        not check and changed and loaded
    )


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("account", ["agent", "builder"])
def test_planned_account_guard(*, check: bool, new: bool, account: str) -> None:
    main = MAIN
    for task in main[1:3]:
        assert conditions(
            task["when"],
            ansible_check_mode=check,
            agent_account_new_user=new,
            agent_reconverge_account=account,
        ) is (check and new and account == "agent")


def test_missing_script_does_not_gate_installation() -> None:
    main = MAIN
    assert main[5]["ansible.builtin.stat"]["path"] == (
        "{{ agent_reconverge_identity.home }}/{{ agent_reconverge_clone }}"
        "/tony/reconverge.sh"
    )
    assert main[6]["when"] == "not agent_reconverge_script.stat.exists"
    assert main[7]["when"] == "ansible_facts.system == 'Linux'"
    assert main[8]["when"] == "ansible_facts.system == 'Darwin'"


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "agent_reconverge_account": "builder",
            "agent_reconverge_interval_seconds": 60,
        },
        {"agent_reconverge_account": "bad user"},
        {"agent_reconverge_clone": "/absolute"},
        {"agent_reconverge_clone": "../outside"},
        {"agent_reconverge_clone": "Code/../outside"},
        {"agent_reconverge_clone": "Code/$(command)"},
        {"agent_reconverge_interval_seconds": 0},
        {"agent_reconverge_interval_seconds": -1},
        {"ansible_facts": {"system": "Windows"}},
    ],
)
def test_input_validation(overrides: dict[str, object]) -> None:
    variables = (
        DEFAULTS
        | {
            "ansible_facts": {"system": "Linux"},
            "agent_reconverge_clone": "Code/example-account",
        }
        | overrides
    )
    templar = Templar(loader=DataLoader(), variables=variables)
    assertions = MAIN[0]["ansible.builtin.assert"]["that"]
    results = [
        templar.evaluate_conditional(trust_as_template(expression))
        for expression in assertions
    ]
    assert all(results) is (
        overrides
        in (
            {},
            {
                "agent_reconverge_account": "builder",
                "agent_reconverge_interval_seconds": 60,
            },
        )
    )
