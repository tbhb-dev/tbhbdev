# cspell:ignore publickey
import socket
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import jinja2
import pytest
import yaml
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import filter_loader, init_plugin_loader
from ansible.template import Templar
from ansible.vars.manager import VariableManager
from jinja2.nativetypes import NativeEnvironment
from tbhbops import dsm_accounts as da
from tbhbops.authorized_lines import authorized_lines
from tbhbops.tailnet_addresses import RANGES

from .ssh_argv import SSH_NO_AGENT_ARGV

if TYPE_CHECKING:
    from ansible.inventory.host import Host

FULL = "rwxpdDaARWcCo"
USERS = (
    "root:x:0:0::/root:/bin/ash\n"
    "agent:x:1036:100::/var/services/homes/agent:/bin/sh\n"
    "ops:x:1037:100::/var/services/homes/ops:/bin/sh\n"
)
GROUP = "administrators:x:101:admin,agent,ops\ndocker:x:65537:agent,ops\nusers:x:100:\n"


def _gaps(
    users: str = USERS,
    group: str = GROUP,
    accounts: tuple[str, ...] = ("agent", "ops"),
    groups: tuple[str, ...] = ("administrators", "docker"),
) -> list[str]:
    return da.account_gaps(users, group, accounts, groups, "/bin/sh")


def test_accounts_that_meet_the_requirements_have_no_gaps() -> None:
    assert _gaps() == []


def test_a_missing_account_gets_one_gap() -> None:
    assert _gaps(accounts=("agent", "ghost")) == ["ghost: no such account"]


def test_a_missing_account_does_not_stop_the_check_of_the_next() -> None:
    users = USERS.replace("ops:/bin/sh", "ops:/sbin/nologin")
    assert _gaps(users, accounts=("ghost", "ops")) == [
        "ghost: no such account",
        "ops: shell is /sbin/nologin, not /bin/sh",
    ]


def test_a_wrong_shell_is_a_gap() -> None:
    users = USERS.replace("ops:/bin/sh", "ops:/sbin/nologin")
    assert _gaps(users) == ["ops: shell is /sbin/nologin, not /bin/sh"]


def test_missing_group_membership_is_listed_per_group() -> None:
    group = "administrators:x:101:admin\ndocker:x:65537:agent\n"
    assert _gaps(group=group) == [
        "agent: not in administrators",
        "ops: not in administrators",
        "ops: not in docker",
    ]


def test_a_missing_group_counts_as_no_membership() -> None:
    assert _gaps(group="administrators:x:101:agent,ops\n") == [
        "agent: not in docker",
        "ops: not in docker",
    ]


def test_a_name_that_only_contains_the_account_is_not_a_member() -> None:
    group = "administrators:x:101:agents,sops\ndocker:x:65537:agent,ops\n"
    assert _gaps(group=group) == [
        "agent: not in administrators",
        "ops: not in administrators",
    ]


def test_short_lines_are_ignored() -> None:
    assert _gaps(users=USERS + "broken:x:1\n", group=GROUP + "broken:x\n") == []
    assert _gaps(users="agent:x:1:2:3:4\n", accounts=("agent",)) == [
        "agent: no such account"
    ]
    assert _gaps(group="docker:x:1\n", accounts=("agent",), groups=("docker",)) == [
        "agent: not in docker"
    ]


def _acl(*entries: str) -> str:
    return "".join(f"[{i}] {entry}\n" for i, entry in enumerate(entries))


DENIES = (f"user:agent:deny:{FULL}:fd--", f"user:ops:deny:{FULL}:fd--")


def test_denies_ahead_of_the_allows_have_no_gaps() -> None:
    acl = "Archive:  /srv/example-storage/Backups\n" + _acl(
        *DENIES, "group:administrators:allow:rwxpdDaARWcCo:fd--"
    )
    assert da.acl_gaps(acl, ["agent", "ops"]) == []


def test_an_acl_of_only_denies_has_no_gaps() -> None:
    assert da.acl_gaps(_acl(*DENIES), ["agent", "ops"]) == []


def test_an_account_with_no_entry_is_a_gap() -> None:
    acl = _acl(DENIES[0], "everyone:allow:r-x---a-R-c--:fd--")
    assert da.acl_gaps(acl, ["agent", "ops"]) == [
        "ops: no deny entry for every permission"
    ]


def test_an_empty_acl_is_a_gap_for_each_account() -> None:
    assert da.acl_gaps("", ["agent", "ops"]) == [
        "agent: no deny entry for every permission",
        "ops: no deny entry for every permission",
    ]


@pytest.mark.parametrize(
    "entry",
    [
        f"user:agent:allow:{FULL}:fd--",
        f"group:agent:deny:{FULL}:fd--",
        f"user:agents:deny:{FULL}:fd--",
        f"user:agent2:deny:{FULL}:fd--",
        "user:agent:deny:rwxpdDaARWcC:fd--",
        "user:agent:deny:rwxpdDaARWcC-:fd--",
    ],
)
def test_an_entry_that_is_not_a_full_user_deny_does_not_count(entry: str) -> None:
    assert da.acl_gaps(_acl(entry), ["agent"]) == [
        "agent: no deny entry for every permission"
    ]


def test_a_deny_after_an_allow_is_a_gap() -> None:
    acl = _acl(DENIES[0], "user:tony:allow:rwx:fd--", DENIES[1])
    assert da.acl_gaps(acl, ["agent", "ops"]) == [
        "ops: deny entry sits after an allow entry"
    ]


def test_a_deny_directly_after_the_last_entry_position_counts_as_late() -> None:
    acl = _acl("user:tony:allow:rwx:fd--", DENIES[0])
    assert da.acl_gaps(acl, ["agent"]) == [
        "agent: deny entry sits after an allow entry"
    ]


def test_a_second_deny_after_an_allow_does_not_hide_a_late_first_one() -> None:
    acl = _acl("user:tony:allow:rwx:fd--", DENIES[0], DENIES[0])
    assert da.acl_gaps(acl, ["agent"]) == [
        "agent: deny entry sits after an allow entry"
    ]


def test_extra_permission_letters_are_fine() -> None:
    acl = _acl(f"user:agent:deny:{FULL}x:fd--")
    assert da.acl_gaps(acl, ["agent"]) == []


ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "roles").is_dir()
)
ROLE = ANSIBLE / "roles" / "dsm_accounts"
needs_inventory = pytest.mark.skipif(
    not (ANSIBLE / "inventory/hosts.yml").is_file(),
    reason="requires the private Ansible inventory",
)


@needs_inventory
def test_nas_uses_the_same_controller_session_key_as_lab() -> None:
    group_vars = ANSIBLE / "inventory" / "group_vars"
    nas_text = (group_vars / "nas.yml").read_text(encoding="utf-8")
    nas = yaml.safe_load(nas_text)
    lab = yaml.safe_load(
        (group_vars / "all.yml").read_text(encoding="utf-8")
    ) | yaml.safe_load((group_vars / "lab.yml").read_text(encoding="utf-8"))
    assert nas["ansible_ssh_extra_args"] == lab["ansible_ssh_extra_args"]
    assert "lab-ops" not in nas_text


@needs_inventory
def test_nas_ops_keys_select_only_the_settled_controller_keys() -> None:
    group_vars = ANSIBLE / "inventory" / "group_vars"
    context = yaml.safe_load((group_vars / "all.yml").read_text()) | yaml.safe_load(
        (group_vars / "nas.yml").read_text()
    )
    keys = context["ssh_controller_keys"]
    resolved = []

    def resolve(
        host: str, port: object, **kwargs: int
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del port, kwargs
        resolved.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (str(RANGES[0][1]), 0))]

    def controller_lines(selected: dict[str, str]) -> list[str]:
        return authorized_lines(selected, None, resolve)

    environment = NativeEnvironment(undefined=jinja2.StrictUndefined)
    cast("dict[str, object]", environment.filters)["authorized_lines"] = (
        controller_lines
    )
    lines = environment.from_string(context["dsm_accounts_ops_keys"]).render(context)
    assert lines == [
        f'from="{RANGES[0][1]}" {keys[host]}' for host in ["mac-a", "mac-b"]
    ]
    assert resolved == ["mac-a", "mac-b"]
    assert all(keys["host-f"] not in line for line in lines)


def test_ops_keys_preserves_existing_lines_and_ssh_directory() -> None:
    (task,) = yaml.safe_load((ROLE / "tasks" / "ops_keys.yml").read_text())
    assert task["ansible.posix.authorized_key"] == {
        "user": "ops",
        "key": "{{ dsm_accounts_ops_keys | join('\n') }}",
        "exclusive": False,
        "manage_dir": False,
    }
    assert task["become"] is False
    assert set(task) == {"name", "ansible.posix.authorized_key", "become"}


@needs_inventory
def test_ops_keys_renders_two_restricted_lines_with_ansible() -> None:
    # Load the real NAS host vars and filter plugin without contacting a host.
    # Only DNS is stubbed, so this also runs offline in the Bazel sandbox.
    init_plugin_loader()
    filter_loader.add_directory(str(ANSIBLE / "plugins" / "filter"))
    loader = DataLoader()
    inventory = InventoryManager(
        loader=loader, sources=[str(ANSIBLE / "inventory" / "hosts.yml")]
    )
    host: Host = inventory.groups["nas"].get_hosts()[0]
    variables = VariableManager(loader=loader, inventory=inventory).get_vars(host=host)
    (task,) = loader.load_from_file(
        str(ROLE / "tasks" / "ops_keys.yml"), trusted_as_template=True
    )
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", (str(RANGES[0][1]), 0))
    with patch("socket.getaddrinfo", return_value=[address]) as resolve:
        rendered = Templar(loader=loader, variables=variables).template(
            task["ansible.posix.authorized_key"]["key"]
        )

    # Keep key bodies and from= addresses out of pytest's failure output.
    lines = rendered.splitlines()
    line_count = len(lines)
    assert line_count == 2
    restricted = [line.startswith("from=") for line in lines]
    assert restricted == [True, True]
    has_literal_backslash_n = "\\n" in rendered
    assert not has_literal_backslash_n
    assert [call.args[0] for call in resolve.call_args_list] == [
        "mac-a",
        "mac-b",
    ]


def test_ops_keys_is_opt_in_after_the_password_import() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    password, keys = tasks[-3:-1]
    assert password["ansible.builtin.import_tasks"] == "set_password.yml"
    assert keys["ansible.builtin.import_tasks"] == "ops_keys.yml"
    assert keys["tags"] == ["never", "dsm_accounts_ops_keys"]
    assert set(keys) == {"name", "ansible.builtin.import_tasks", "tags"}


def test_ops_keys_defaults_to_restricted_controller_keys() -> None:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults["dsm_accounts_ops_keys"] == (
        "{{ ssh_controller_keys | authorized_lines }}"
    )
    spec = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())
    option = spec["argument_specs"]["main"]["options"]["dsm_accounts_ops_keys"]
    assert option["type"] == "list"
    assert option["elements"] == "str"


def test_retirement_is_opt_in_after_authorizing_session_keys() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    assert tasks[-1] == {
        "name": "Retire the shared lab-ops key after proving controller access",
        "ansible.builtin.import_tasks": "ops_retire_lab_ops.yml",
        "tags": ["never", "dsm_accounts_ops_retire_lab_ops"],
    }
    assert all("tags" not in task for task in tasks[:-4])
    assert all("never" in task["tags"] for task in tasks[-4:])


def test_retirement_renders_only_the_exact_retired_key_as_absent(
    tmp_path: Path,
) -> None:
    retired_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMBxMcLBO2dNsuwi0clAzEHghsyOUtwtQU2PXYShQKQa lab-ops"
    key_file = tmp_path / "lab-ops.pub"
    key_file.write_text(f"{retired_key}\n", encoding="utf-8")
    init_plugin_loader()
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks" / "ops_retire_lab_ops.yml"), trusted_as_template=True
    )
    removal = tasks[5]["ansible.posix.authorized_key"]
    rendered = Templar(
        loader=loader, variables={"dsm_accounts_retired_key_src": str(key_file)}
    ).template(removal)
    assert set(rendered) == {"user", "key", "state", "manage_dir"}
    assert rendered["user"] == "ops"
    assert rendered["state"] == "absent"
    assert rendered["manage_dir"] is False
    assert rendered["key"].splitlines() == [retired_key]


def test_retirement_never_sets_exclusive_or_uses_become() -> None:
    text = (ROLE / "tasks" / "ops_retire_lab_ops.yml").read_text()
    tasks = yaml.safe_load(text)
    assert all(task["become"] is False for task in tasks)
    assert "exclusive" not in text


@pytest.mark.parametrize(
    ("args", "user", "allowed"),
    [
        ("-i /session-key", "ops", True),
        ("-i ~/.ssh/lab-ops", "ops", False),
        ("-i /session-key -i ~/.ssh/lab-ops", "ops", False),
        ("-i /session-key -i ~/.ssh/LAB-OPS", "ops", False),
        ("-i /another-key", "ops", False),
        ("-i /session-key", "root", False),
    ],
)
def test_retirement_pre_proof_rejects_lab_ops_override(
    args: str, user: str, allowed: bool
) -> None:
    tasks = DataLoader().load_from_file(
        str(ROLE / "tasks" / "ops_retire_lab_ops.yml"), trusted_as_template=True
    )
    templar = Templar(
        loader=DataLoader(),
        variables={
            "ansible_ssh_extra_args": args,
            "ssh_controller_key_path": "/session-key",
            "ansible_user": user,
        },
    )
    conditions = tasks[0]["ansible.builtin.assert"]["that"]
    assert (
        all(templar.evaluate_expression(condition) for condition in conditions)
        is allowed
    )


def test_retirement_checks_shared_key_file_before_removal_and_refusal() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "ops_retire_lab_ops.yml").read_text())
    stat, assertion, removal, refusal = tasks[3:7]
    assert stat["ansible.builtin.stat"] == {
        "path": "{{ '~/.ssh/lab-ops' | expanduser }}",
        "get_checksum": False,
        "get_mime": False,
    }
    argv = refusal["ansible.builtin.command"]["argv"]
    assert stat["ansible.builtin.stat"]["path"] == argv[argv.index("-i") + 1]
    assert stat["register"] == "dsm_accounts_ops_lab_ops_key"
    assert assertion["ansible.builtin.assert"]["that"] == [
        "dsm_accounts_ops_lab_ops_key.stat.exists",
        "dsm_accounts_ops_lab_ops_key.stat.isreg | default(false)",
    ]
    assert "ansible.posix.authorized_key" in removal
    for task in [stat, assertion]:
        assert task["delegate_to"] == "localhost"
        assert task["become"] is False
        assert task["check_mode"] is False
        assert "when" not in task


@pytest.mark.parametrize(
    ("stat", "allowed"),
    [
        ({"exists": False}, False),
        ({"exists": True, "isreg": False}, False),
        ({"exists": True, "isreg": True}, True),
    ],
)
def test_retirement_requires_an_existing_regular_shared_key_file(
    stat: dict[str, bool], allowed: bool
) -> None:
    tasks = DataLoader().load_from_file(
        str(ROLE / "tasks" / "ops_retire_lab_ops.yml"), trusted_as_template=True
    )
    templar = Templar(
        loader=DataLoader(), variables={"dsm_accounts_ops_lab_ops_key": {"stat": stat}}
    )
    conditions = tasks[4]["ansible.builtin.assert"]["that"]
    assert (
        all(templar.evaluate_expression(condition) for condition in conditions)
        is allowed
    )


def test_retirement_proofs_surround_the_removal_and_use_fresh_sessions() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "ops_retire_lab_ops.yml").read_text())
    before, removal, refusal, after = (tasks[i] for i in [2, 5, 6, 8])
    assert "ansible.posix.authorized_key" in removal
    for task, stage in [(before, "before"), (after, "after")]:
        assert task["ansible.builtin.command"]["argv"] == ["id", "-un"]
        assert task["check_mode"] is False
        assert task["changed_when"] is False
        assert task["failed_when"] == (
            f"dsm_accounts_ops_session_{stage}.rc != 0 or "
            f"dsm_accounts_ops_session_{stage}.stdout != 'ops'"
        )
    assert tasks[1]["ansible.builtin.meta"] == "reset_connection"
    assert tasks[7]["ansible.builtin.meta"] == "reset_connection"
    assert refusal["delegate_to"] == "localhost"
    assert refusal["when"] == "not ansible_check_mode"
    assert refusal["changed_when"] is False
    assert refusal["ansible.builtin.command"]["argv"] == [
        *SSH_NO_AGENT_ARGV,
        "-o",
        "ConnectTimeout=10",
        "-i",
        "{{ '~/.ssh/lab-ops' | expanduser }}",
        "-o",
        "HostKeyAlias={{ inventory_hostname }}",
        "-o",
        "UserKnownHostsFile={{ playbook_dir }}/../known_hosts",
        "-o",
        "StrictHostKeyChecking=yes",
        "-l",
        "ops",
        "{{ hostvars[inventory_hostname].ansible_host | default(inventory_hostname) }}",
        "true",
    ]


@pytest.mark.parametrize(
    ("rc", "stderr", "failed"),
    [
        (255, "ops@nas: Permission denied (publickey,password).", False),
        (0, "", True),
        (255, "Connection timed out", True),
        (255, "Host key verification failed.", True),
        (255, "Warning: Identity file missing", True),
    ],
)
def test_retirement_requires_publickey_refusal(
    rc: int, stderr: str, failed: bool
) -> None:
    tasks = DataLoader().load_from_file(
        str(ROLE / "tasks" / "ops_retire_lab_ops.yml"), trusted_as_template=True
    )
    templar = Templar(
        loader=DataLoader(),
        variables={"dsm_accounts_ops_lab_ops_refusal": {"rc": rc, "stderr": stderr}},
    )
    assert templar.evaluate_expression(tasks[6]["failed_when"]) is failed
