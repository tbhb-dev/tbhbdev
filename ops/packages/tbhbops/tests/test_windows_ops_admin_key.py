"""Keep the Windows administrators' key file append-only.

Windows OpenSSH reads every administrator's keys from one file, and that file
holds other people's keys. The `windows_accounts` role may add the ops keys to it
as individual lines and may do nothing else to it. These tests read the role and the
inventory. They fail when a task would replace, template, copy or re-permission
that file, or when the ops keys drift from the inventory.

They can't run the module. Molecule here runs Linux containers, and
`win_lineinfile` is PowerShell, so nothing here shows that existing lines
survive byte for byte or that a second run reports no change. The role's README
and the pull request name the check to run on the host instead.
"""

import re
from pathlib import Path
from typing import cast
lazy from collections.abc import Iterable, Iterator

import jinja2
import pytest
import yaml

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "roles").is_dir()
)
ROLE = ANSIBLE / "roles" / "windows_accounts"
MODULE = "ansible.windows.win_lineinfile"
KEY_VARIABLE = "windows_accounts_ops_admin_keys"
CLEAN_KEY_TASK = "Require clean authorized key lists"
# The one task's arguments, exactly. A missing regexp, backrefs, insertbefore,
# backup or validate is part of the pin, and so is create: false.
ARGUMENTS: dict[object, object] = {
    "path": "C:\\ProgramData\\ssh\\administrators_authorized_keys",
    "line": "{{ lookup('ansible.builtin.template', 'authorized_keys.j2') | trim }}",
    "state": "present",
    "create": False,
    "newline": "windows",
}
# Keywords that leave the task's outcome alone. A changed_when, failed_when,
# ignore_errors or block could hide a failure. The loop and its template vars
# are checked separately, so each iteration adds exactly one line.
HARMLESS_KEYWORDS = {"name", "when", "tags"}
ITERATION = {
    "loop": "{{ " + KEY_VARIABLE + " }}",
    "vars": {"windows_accounts_keys": "{{ [item] }}"},
    "loop_control": {"label": "{{ item.key }}"},
}
# The file's name, and the directory it sits in, so a task that reaches the
# file by another name for its folder is found too.
KEY_FILE = re.compile(r"administrators_authorized_keys|programdata", re.IGNORECASE)
OPS_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGuQMbLm4j7XFGVUBIm8GcsuCEwjzrskD+sE55hQE9r6"
    " lab-ops"
)


def _load(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _text(value: object) -> str:
    """A parsed YAML value as text, so a search sees every string nested in it."""
    return yaml.safe_dump(value)


def _mapping(value: object) -> dict[object, object]:
    assert isinstance(value, dict)
    mapping: dict[object, object] = value
    return mapping


def _tasks(value: object) -> Iterator[dict[object, object]]:
    """Every task in a parsed task list, however deep its blocks nest."""
    if not isinstance(value, list):
        return
    items: list[object] = value
    for item in items:
        if not isinstance(item, dict):
            continue
        task: dict[object, object] = item
        parts = [task[key] for key in ("block", "rescue", "always") if key in task]
        if parts:
            for part in parts:
                yield from _tasks(part)
        else:
            yield task


def _role_tasks() -> list[dict[object, object]]:
    return [
        task
        for path in sorted((ROLE / "tasks").glob("*.yml"))
        for task in _tasks(_load(path))
    ]


def _touches_key_file(task: dict[object, object]) -> bool:
    """Whether any value of a task, apart from its name, names the key file."""
    return any(
        KEY_FILE.search(_text(value)) for key, value in task.items() if key != "name"
    )


def violations(tasks: list[dict[object, object]]) -> list[str]:
    """How a task list strays from one append-only line per entry."""
    touching = [task for task in tasks if _touches_key_file(task)]
    if len(touching) != 1:
        return [f"{len(touching)} tasks touch the key file, and exactly 1 may"]
    task = touching[0]
    found = [
        f"keyword {key!r} changes how the task fails or repeats"
        for key in sorted(
            map(str, set(task) - HARMLESS_KEYWORDS - {MODULE} - set(ITERATION))
        )
    ]
    found.extend(
        f"{key} does not render exactly one key per entry"
        for key, value in ITERATION.items()
        if task.get(key) != value
    )
    arguments = task.get(MODULE)
    if arguments != ARGUMENTS:
        found.append(
            f"the task is not {MODULE} with exactly {ARGUMENTS!r}: {arguments!r}"
        )
    return found


def _the_task() -> dict[object, object]:
    return next(task for task in _role_tasks() if _touches_key_file(task))


def test_the_role_tasks_are_found() -> None:
    assert {task.get("name") for task in _role_tasks()} >= {
        "Require a Windows host",
        CLEAN_KEY_TASK,
        "Put ops in Administrators",
        "Restrict agent's key file to agent, Administrators and SYSTEM",
    }


def test_the_role_adds_one_line_per_entry_and_nothing_else() -> None:
    assert violations(_role_tasks()) == []


def test_the_task_has_no_regexp_and_no_backrefs() -> None:
    arguments = _the_task()[MODULE]
    assert isinstance(arguments, dict)
    assert not {"regexp", "regex", "backrefs"} & set(arguments)


def test_the_task_does_not_create_the_file() -> None:
    arguments = _the_task()[MODULE]
    assert isinstance(arguments, dict)
    assert arguments["create"] is False
    assert arguments["state"] == "present"


def test_no_other_file_of_the_role_names_the_key_file() -> None:
    others = [
        path
        for path in sorted(ROLE.rglob("*"))
        if path.is_file()
        and path.relative_to(ROLE).parts[0] not in {"tasks", "meta"}
        and path.name != "README.md"
    ]
    assert {path.name for path in others} >= {"authorized_keys.j2", "restrict-acl.ps1"}
    assert [
        path.name for path in others if KEY_FILE.search(path.read_text("utf-8"))
    ] == []


def test_the_argument_spec_requires_ops_key_entries_with_optional_hosts() -> None:
    specs = _mapping(_load(ROLE / "meta" / "argument_specs.yml"))
    main = _mapping(_mapping(specs["argument_specs"])["main"])
    option = _mapping(_mapping(main["options"])[KEY_VARIABLE])
    assert (option["type"], option["elements"], option["required"]) == (
        "list",
        "dict",
        True,
    )
    options = _mapping(option["options"])
    assert _mapping(options["key"])["required"] is True
    assert _mapping(options["from"]) == {
        "type": "list",
        "elements": "str",
        "default": [],
        "description": _mapping(options["from"])["description"],
    }


def _clean_key_check(keys: list[dict[str, object]]) -> bool:
    """Evaluate every role assertion on the ops list, without host access."""
    task = next(task for task in _role_tasks() if task.get("name") == CLEAN_KEY_TASK)
    conditions = task["ansible.builtin.assert"]
    assert isinstance(conditions, dict)
    that: list[object] = conditions["that"]
    loop = cast("list[dict[str, object]]", task["loop"])
    ops = next(item for item in loop if item["name"] == "ops")
    assert ops["entries"] == "{{ " + KEY_VARIABLE + " }}"
    loop_var = str(_mapping(task["loop_control"])["loop_var"])

    def match(value: object, pattern: str) -> bool:
        return isinstance(value, str) and re.match(pattern, value) is not None

    def flatten(lists: Iterable[list[str]]) -> list[str]:
        return [host for hosts in lists for host in hosts]

    environment = jinja2.Environment(autoescape=True)
    cast("dict[str, object]", environment.tests)["match"] = match
    cast("dict[str, object]", environment.filters)["flatten"] = flatten
    return all(
        environment.compile_expression(str(condition))(
            **{loop_var: ops | {"entries": keys}}
        )
        for condition in that
    )


@pytest.mark.parametrize(
    "key",
    [
        OPS_KEY,
        "ssh-ed25519 AAAA+/= someone@host",
        "ssh-ed25519 AAAA",
    ],
)
def test_a_clean_ops_key_passes_the_assertion(key: str) -> None:
    assert _clean_key_check([{"key": key}, {"key": key, "from": ["mac-a"]}])


@pytest.mark.parametrize(
    "key",
    [
        "",
        "ssh-ed25519",
        "ssh-ed25519 !!!",
        "ssh-rsa AAAA",
        "ecdsa-sha2-nistp256 AAAA",
        'from="x" ' + OPS_KEY,
        " " + OPS_KEY,
        OPS_KEY + "\n",
        OPS_KEY + "\r\n",
        OPS_KEY + "\n" + OPS_KEY,
        OPS_KEY + "\r" + OPS_KEY,
    ],
)
def test_a_key_with_options_or_a_second_line_fails_the_assertion(key: str) -> None:
    assert not _clean_key_check([{"key": OPS_KEY}, {"key": key}])


def test_an_empty_ops_list_fails_the_assertion() -> None:
    assert not _clean_key_check([])


@pytest.mark.parametrize("host", ["", "A", "-a", "a-", "a b", 'a"', "a,b", "a\n"])
def test_an_invalid_from_host_fails_the_assertion(host: str) -> None:
    assert not _clean_key_check([{"key": OPS_KEY, "from": [host]}])


def test_ops_and_agent_share_the_template_and_address_resolution() -> None:
    environment = jinja2.Environment(autoescape=True)
    resolved: list[list[str]] = []

    def addresses(hosts: list[str]) -> list[str]:
        resolved.append(hosts)
        return ["192.0.2.1", "2001:db8::1"]

    cast("dict[str, object]", environment.filters)["tailnet_addresses"] = addresses
    template = environment.from_string(
        (ROLE / "templates" / "authorized_keys.j2").read_text("utf-8")
    )
    assert template.render(windows_accounts_keys=[{"key": OPS_KEY}]).strip() == OPS_KEY
    assert (
        template.render(
            windows_accounts_keys=[
                {
                    "key": OPS_KEY,
                    "from": ["mac-a"],
                }
            ]
        ).strip()
        == f'from="192.0.2.1,2001:db8::1" {OPS_KEY}'
    )
    assert resolved == [["mac-a"]]
    agent = next(
        task for task in _role_tasks() if task.get("name") == "Authorize agent's keys"
    )
    assert (
        _mapping(agent["ansible.windows.win_template"])["src"] == "authorized_keys.j2"
    )


def _task(**changes: object) -> dict[object, object]:
    """The role's task as it stands, with arguments or keywords changed."""
    task: dict[object, object] = {
        "name": "Add the key",
        MODULE: dict(ARGUMENTS),
        **ITERATION,
    }
    task.update(changes)
    return task


def _with(**arguments: object) -> dict[object, object]:
    return _task(**{MODULE: dict(ARGUMENTS) | arguments})


@pytest.mark.parametrize(
    "task",
    [
        _with(regexp="^ssh-ed25519 .* lab-ops$"),
        _with(regex=".*"),
        _with(regexp="^ssh-ed25519", backrefs=True),
        _with(create=True),
        _with(state="absent"),
        _with(insertbefore="BOF"),
        _with(backup=True),
        _with(validate="echo %s"),
        _with(newline="unix"),
        _with(path="C:\\ProgramData\\ssh\\sshd_config"),
        _with(line="{{ other_key }}"),
        _task(**{"ansible.windows.win_lineinfile": "path=x line=y"}),
        _task(**{MODULE: None}),
        _task(failed_when=False),
        _task(changed_when=False),
        _task(ignore_errors=True),
        _task(loop=[1, 2]),
        _task(vars={"windows_accounts_keys": "{{ windows_accounts_ops_admin_keys }}"}),
        _task(loop_control={"label": "other"}),
    ],
    ids=lambda task: str(task)[:80],
)
def test_a_task_that_strays_from_the_one_line_is_found(
    task: dict[object, object],
) -> None:
    assert violations([task]) != []


@pytest.mark.parametrize(
    "task",
    [
        {
            "ansible.windows.win_template": {
                "src": "authorized_keys.j2",
                "dest": "C:\\ProgramData\\ssh\\administrators_authorized_keys",
            }
        },
        {
            "ansible.windows.win_copy": {
                "content": "x",
                "dest": "C:/ProgramData/ssh/ADMINISTRATORS_AUTHORIZED_KEYS",
            }
        },
        {
            "ansible.windows.win_acl": {
                "path": "C:\\ProgramData\\ssh\\administrators_authorized_keys"
            }
        },
        {
            "ansible.windows.win_powershell": {
                "script": "x",
                "parameters": {
                    "Path": ["C:\\ProgramData\\ssh\\administrators_authorized_keys"]
                },
            }
        },
        {
            "ansible.windows.win_file": {
                "path": "C:\\ProgramData\\ssh",
                "state": "absent",
            }
        },
    ],
)
def test_a_second_task_on_the_file_is_found(task: dict[object, object]) -> None:
    assert violations([_task(), {"name": "Another", **task}]) != []
    assert violations([task]) != []


def test_a_task_on_another_file_is_left_alone() -> None:
    other: dict[object, object] = {
        "ansible.windows.win_file": {
            "path": "C:\\Users\\example-user\\.ssh",
            "state": "directory",
        }
    }
    assert violations([_task(), other]) == []
    assert violations([_task(when="x is defined", tags=["keys"])]) == []


def test_a_name_that_mentions_the_file_is_not_a_touch() -> None:
    assert violations([_task(name="Mention administrators_authorized_keys")]) == []


def test_a_task_inside_a_block_is_found() -> None:
    nested = [{"block": [{"rescue": [_task()]}]}]
    assert list(_tasks(nested)) == [_task()]
    assert list(_tasks("not a list")) == []
    assert list(_tasks([7, _task(name="plain")])) == [_task(name="plain")]
