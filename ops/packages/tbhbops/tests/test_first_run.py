"""Check the first-run artifacts against the ops_accounts role.

The lab nodes' autoinstall file comes from a role template, and the Macs' script
is plain shell. Both create ops the way the role does, so these tests read the
role's own values and fail when either artifact drifts from them. The script runs
against stand-ins for dscl, softwareupdate and the other macOS tools, in a
scratch root, and never touches the machine that runs the tests.
"""

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = next(
    path
    for path in Path(__file__).parents
    if (path / "ansible").is_dir() and (path / "packages" / "tbhbops").is_dir()
)
ROLE = ROOT / "ansible" / "roles" / "ops_accounts"
SCRIPT = ROOT / "ansible" / "scripts" / "first-run-macos.sh"
needs_inventory = pytest.mark.skipif(
    not (ROOT / "ansible/inventory/group_vars/agent_hosts.yml").is_file(),
    reason="requires the private agent_hosts.yml inventory",
)
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMBxMcLBO2dNsuwi0clAzEHghsyOUtwtQU2PXYShQKQa"
LABEL = "Command Line Tools for Xcode 27.2-27.2"
MARKER = "tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
# What softwareupdate --list prints on macOS 27.2, with the marker in place.
OFFER = f"""\
Software Update Tool

Finding available software
Software Update found the following new or updated software:
* Label: {LABEL}
\tTitle: Command Line Tools for Xcode 27.2, Version: 27.2, Size: 521120KiB,\
 Recommended: YES,
"""
NONE_OFFERED = "No new software available.\n"


@dataclass
class Db:
    """The account database the stand-in tools share."""

    users: dict[str, int]
    groups: dict[str, int]
    members: list[str]


def at(node: object, *path: str | int) -> object:
    """Return the value at a path of keys and indexes in parsed YAML."""
    for step in path:
        if isinstance(node, dict) and isinstance(step, str):
            # A separate branch keeps each type narrowed for its own index.
            node = node[step]
        elif isinstance(node, list) and isinstance(step, int):
            node = node[step]
        else:
            msg = f"no {step!r} in {node!r}"
            raise TypeError(msg)
    return node


def load(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def ops_id() -> int:
    value = at(load(ROLE / "vars" / "main.yml"), "ops_accounts_id")
    assert isinstance(value, int)
    return value


def sudoers_line() -> str:
    value = at(load(ROLE / "vars" / "main.yml"), "ops_accounts_sudoers_line")
    assert isinstance(value, str)
    return value


def lab_keys() -> list[str]:
    path = ROOT / "ansible" / "inventory" / "group_vars" / "agent_hosts.yml"
    value = at(load(path), "ops_lab_ops_keys")
    assert isinstance(value, list)
    items: list[object] = value
    assert all(isinstance(key, str) for key in items)
    return [key for key in items if isinstance(key, str)]


def render(keys: list[str] | None = None) -> str:
    """Render the autoinstall template the way Ansible's template module does."""
    env = Environment(  # noqa: S701 - the output is YAML, not HTML
        undefined=StrictUndefined, trim_blocks=True, keep_trailing_newline=True
    )
    source = (ROLE / "templates" / "autoinstall-user-data.yml.j2").read_text(
        encoding="utf-8"
    )
    return env.from_string(source).render(
        ops_accounts_id=ops_id(),
        ops_accounts_sudoers_line=sudoers_line(),
        inventory_hostname="host-a",
        ops_accounts_admin_keys=keys if keys is not None else lab_keys(),
    )


def user_data(text: str) -> object:
    return at(yaml.safe_load(text), "autoinstall", "user-data")


def task(name: str) -> object:
    """Return one of the role's tasks by name."""
    tasks = load(ROLE / "tasks" / "main.yml")
    assert isinstance(tasks, list)
    items: list[object] = tasks
    return next(item for item in items if at(item, "name") == name)


@needs_inventory
def test_autoinstall_matches_the_role() -> None:
    text = render()
    assert text.startswith("#cloud-config\n")
    data = user_data(text)

    assert at(data, "users") == [
        {
            "name": "ops",
            "uid": ops_id(),
            "primary_group": "ops",
            "shell": "/bin/bash",
            "lock_passwd": True,
            "ssh_authorized_keys": lab_keys(),
        }
    ]
    assert at(data, "bootcmd") == [
        [
            "cloud-init-per",
            "once",
            "ops-group",
            "groupadd",
            "--gid",
            str(ops_id()),
            "ops",
        ]
    ]
    assert at(data, "hostname") == "host-a"
    assert at(data, "write_files") == [
        {
            "path": "/etc/sudoers.d/ops",
            "owner": "root:root",
            "permissions": "0440",
            "content": sudoers_line() + "\n",
        }
    ]


@needs_inventory
def test_autoinstall_keeps_the_install_closed() -> None:
    autoinstall = at(yaml.safe_load(render()), "autoinstall")
    assert at(autoinstall, "ssh") == {"install-server": True, "allow-pw": False}
    # An identity section would add a second account with a password.
    assert isinstance(autoinstall, dict)
    assert "identity" not in autoinstall
    assert at(autoinstall, "version") == 1


def test_autoinstall_survives_awkward_keys() -> None:
    keys = [f'{KEY} it\'s a "quoted" <comment> & more', f"{KEY} second"]
    data = user_data(render(keys))
    assert at(data, "users", 0, "ssh_authorized_keys") == keys


def test_role_tasks_agree_with_the_artifacts() -> None:
    """The role's own tasks use the vars and settings the artifacts copy."""
    assert at(task("Grant ops full sudo"), "ansible.builtin.copy", "content") == (
        "{{ ops_accounts_sudoers_line }}\n"
    )
    create = at(task("Create ops"), "ansible.builtin.user")
    assert at(create, "uid") == "{{ ops_accounts_id }}"
    shell = at(create, "shell")
    assert isinstance(shell, str)
    assert "'/bin/bash'" in shell
    lock = at(create, "password_lock")
    assert isinstance(lock, str)
    assert lock.startswith("{{ true if ansible_facts.os_family == 'Debian'")
    assert at(task("Create the ops group"), "ansible.builtin.group", "gid") == (
        "{{ ops_accounts_id }}"
    )


def test_script_constants_match_the_role() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    script_id = re.search(r"^ops_id=(\d+)$", text, re.MULTILINE)
    sudoers = re.search(r"^sudoers_line='(.*)'$", text, re.MULTILINE)
    assert script_id
    assert sudoers
    assert int(script_id.group(1)) == ops_id()
    assert sudoers.group(1) == sudoers_line()


# A stand-in for each macOS tool the script calls. One file serves every name,
# keeps its state in $STUB_STATE, and logs each call to calls.log.
# Resolve runfiles indirection so the generated shebang fits Linux's limit.
STUB = dedent(
    """\
    #!{python}
    import json, os, sys
    from pathlib import Path

    name = Path(sys.argv[0]).name
    args = sys.argv[1:]
    state = Path(os.environ["STUB_STATE"])
    with (state / "calls.log").open("a") as log:
        log.write(" ".join([name, *args]) + "\\n")
    db_path = state / "db.json"
    db = json.loads(db_path.read_text())

    def save():
        db_path.write_text(json.dumps(db))

    if name == "id":
        print(os.environ.get("STUB_UID", "0"))
    elif name == "xcode-select":
        if not (state / "clt").exists():
            sys.exit(2)
        print(state / "dev")
    elif name == "softwareupdate" and args == ["--list"]:
        marker = Path(os.environ["OPS_FIRST_RUN_ROOT"]) / "{marker}"
        offer = (state / "offer.txt").read_text()
        print(offer if marker.exists() else "No new software available.")
    elif name == "softwareupdate":
        if not (state / "install-noop").exists():
            (state / "clt").touch()
    elif name == "dscl" and args[1] == "-list":
        table = db["users" if args[2] == "/Users" else "groups"]
        for key, value in table.items():
            print(f"{{key}}    {{value}}")
    elif name == "dscl" and args[1] == "-read":
        sys.exit(0 if args[2].split("/")[2] in db["groups"] else 56)
    elif name == "dscl" and args[1] == "-create":
        _, kind, record = args[2].split("/")
        if len(args) == 5 and args[3] in ("UniqueID", "PrimaryGroupID"):
            db["users" if kind == "Users" else "groups"][record] = int(args[4])
            save()
    elif name == "dseditgroup" and args[1] == "checkmember":
        sys.exit(0 if args[3] in db["members"] else 67)
    elif name == "dseditgroup":
        db["members"].append(args[3])
        save()
    elif name == "sudo":
        rest = args[3:] if args[:3] == ["-H", "-u", "ops"] else sys.exit(99)
        os.execvp(rest[0], rest)
    elif name == "uv" and args[:2] == ["python", "find"]:
        print(f"/Users/ops/.local/share/uv/python/cpython-{{args[2]}}/bin/python3")
    # chown, visudo and uv python install only log.
    """
).format(python=Path(sys.executable).resolve(), marker=MARKER)


class Mac:
    """A scratch root and a set of stand-in tools for one test."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.keys = tmp_path / "ops-authorized-keys"
        for path in (self.root / "tmp", self.state, self.bin):
            path.mkdir(parents=True)
        python = self.state / "dev" / "usr" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.touch(mode=0o755)
        db = Db(
            users={"root": 0, "tony": 501},
            groups={"wheel": 0, "com.apple.access_ssh": 399},
            members=[],
        )
        (self.state / "db.json").write_text(json.dumps(asdict(db)))
        (self.state / "offer.txt").write_text(OFFER)
        self.keys.write_text(f"{KEY} ops@mac-b\n", encoding="utf-8")
        for name in (
            "id",
            "xcode-select",
            "softwareupdate",
            "dscl",
            "dseditgroup",
            "sudo",
            "uv",
            "chown",
            "visudo",
        ):
            (self.bin / name).write_text(STUB, encoding="utf-8")
            (self.bin / name).chmod(0o755)

    def db(self) -> Db:
        return Db(**json.loads((self.state / "db.json").read_text()))

    def edit(
        self, users: dict[str, int] | None = None, groups: dict[str, int] | None = None
    ) -> None:
        db = self.db()
        db.users = users if users is not None else db.users
        db.groups = groups if groups is not None else db.groups
        (self.state / "db.json").write_text(json.dumps(asdict(db)))

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def run(
        self, *args: str, uid: str = "0", uv: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if not uv:
            (self.bin / "uv").unlink()
        env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "OPS_FIRST_RUN_ROOT": str(self.root),
            "STUB_STATE": str(self.state),
            "STUB_UID": uid,
        }
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def go(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run("--keys", str(self.keys), *extra)

    @property
    def authorized_keys(self) -> Path:
        return self.root / "Users" / "ops" / ".ssh" / "authorized_keys"

    @property
    def sudoers(self) -> Path:
        return self.root / "etc" / "sudoers.d" / "ops"


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    return Mac(tmp_path)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_script_first_run(mac: Mac) -> None:
    result = mac.go()
    assert result.returncode == 0, result.stderr
    assert f"softwareupdate --install {LABEL}" in mac.calls()
    db = mac.db()
    assert db.users["ops"] == 24369
    assert db.groups["ops"] == 24369
    assert db.members == ["ops"]
    assert mac.authorized_keys.read_text() == f"{KEY} ops@mac-b\n"
    assert mode(mac.authorized_keys) == 0o600
    assert mode(mac.authorized_keys.parent) == 0o700
    assert mode(mac.authorized_keys.parent.parent) == 0o700
    assert mac.sudoers.read_text() == sudoers_line() + "\n"
    assert mode(mac.sudoers) == 0o440
    assert f"chown 0:0 {mac.root}/etc/sudoers.d" in mac.calls()
    assert f"chown 24369:24369 {mac.root}/Users/ops" in mac.calls()
    assert "visudo -cf" in "\n".join(mac.calls())
    assert not (mac.root / MARKER).exists()
    assert sorted(path.name for path in mac.sudoers.parent.iterdir()) == ["ops"]
    assert "Done." in result.stdout


def test_script_second_run_changes_nothing(mac: Mac) -> None:
    assert mac.go().returncode == 0
    (mac.state / "calls.log").unlink()
    result = mac.go()
    assert result.returncode == 0, result.stderr
    assert "Command Line Tools are installed" in result.stdout
    assert [call for call in mac.calls() if " -create " in call] == []
    assert not [call for call in mac.calls() if call.startswith("chown")]
    assert "dseditgroup -o edit" not in "\n".join(mac.calls())
    assert not [call for call in mac.calls() if call.startswith("softwareupdate")]


def test_script_replaces_other_keys_and_sudoers(mac: Mac) -> None:
    assert mac.go().returncode == 0
    mac.authorized_keys.write_text("ssh-ed25519 placeholder stray\n")
    mac.sudoers.chmod(0o640)
    mac.sudoers.write_text("ops ALL=(ALL) ALL\n")
    assert mac.go().returncode == 0
    assert mac.authorized_keys.read_text() == f"{KEY} ops@mac-b\n"
    assert mac.sudoers.read_text() == sudoers_line() + "\n"
    assert mode(mac.sudoers) == 0o440


def test_script_dry_run_changes_nothing(mac: Mac) -> None:
    result = mac.go("--dry-run")
    assert result.returncode == 0, result.stderr
    for text in (
        f"would install the Command Line Tools: {LABEL}",
        "would create the ops group (gid 24369)",
        "would create the ops user (uid 24369)",
        "would add ops to com.apple.access_ssh",
        "would create the ops home",
        "would authorize the ops keys",
        "would grant ops sudo",
        "Dry run: nothing changed.",
    ):
        assert text in result.stdout
    assert not (mac.root / "Users").exists()
    assert not (mac.root / "etc").exists()
    assert not (mac.root / MARKER).exists()
    assert not [call for call in mac.calls() if " -create " in call]
    assert not [call for call in mac.calls() if call.startswith(("chown", "visudo"))]
    assert "softwareupdate --install" not in "\n".join(mac.calls())
    assert mac.db().members == []
    # It needs no root, because it only reads.
    assert mac.run("--dry-run", "--keys", str(mac.keys), uid="501").returncode == 0


def test_script_skips_the_label_when_the_tools_are_installed(mac: Mac) -> None:
    (mac.state / "clt").touch()
    result = mac.go()
    assert result.returncode == 0
    assert "Command Line Tools are installed." in result.stdout
    assert "softwareupdate --list" not in mac.calls()


@pytest.mark.parametrize(
    ("offer", "label"),
    [
        (
            (
                "* Label: Command Line Tools for Xcode 26.4-26.4\n"
                "* Label: Command Line Tools for Xcode 27.2-27.2\n"
                "* Label: macOS Tahoe 26.4-26.4\n"
            ),
            "Command Line Tools for Xcode 27.2-27.2",
        ),
        (
            "   * Command Line Tools for Xcode-15.0\n\tTitle: x\n",
            "Command Line Tools for Xcode-15.0",
        ),
    ],
)
def test_script_picks_the_last_label(mac: Mac, offer: str, label: str) -> None:
    (mac.state / "offer.txt").write_text(offer)
    assert mac.go().returncode == 0
    assert f"softwareupdate --install {label}" in mac.calls()


def test_script_stops_when_the_install_leaves_no_tools(mac: Mac) -> None:
    (mac.state / "install-noop").touch()
    result = mac.go()
    assert result.returncode == 1
    assert "not usable" in result.stderr
    assert "ops" not in mac.db().users


def test_script_falls_back_to_uv(mac: Mac) -> None:
    (mac.state / "offer.txt").write_text(NONE_OFFERED)
    result = mac.go()
    assert result.returncode == 0, result.stderr
    assert "offers no Command Line Tools label" in result.stdout
    assert "uv-managed Python 3.13" in result.stdout
    assert "uv python install 3.13" in mac.calls()
    assert (
        "Set ansible_python_interpreter for this host to "
        "/Users/ops/.local/share/uv/python/cpython-3.13/bin/python3"
    ) in result.stdout
    assert mac.db().users["ops"] == 24369


def test_script_fallback_dry_run_installs_nothing(mac: Mac) -> None:
    (mac.state / "offer.txt").write_text(NONE_OFFERED)
    result = mac.go("--dry-run")
    assert result.returncode == 0
    assert "would install a uv-managed Python 3.13 for ops" in result.stdout
    assert "uv python install 3.13" not in mac.calls()


def test_script_fallback_without_uv_changes_nothing(mac: Mac) -> None:
    (mac.state / "offer.txt").write_text(NONE_OFFERED)
    result = mac.run("--keys", str(mac.keys), uv=False)
    assert result.returncode == 1
    assert "uv is not on PATH" in result.stderr
    assert "ops" not in mac.db().users
    assert not (mac.root / "etc").exists()


def test_script_leaves_a_mac_without_the_ssh_group_alone(mac: Mac) -> None:
    mac.edit(groups={"wheel": 0})
    assert mac.go().returncode == 0
    assert "dseditgroup" not in "\n".join(mac.calls())


@pytest.mark.parametrize(
    ("users", "groups", "message"),
    [
        ({"agent": 24369}, {}, "uid 24369 belongs to agent"),
        ({"ops": 600}, {}, "found ops with uid 600"),
        ({}, {"devel": 24369}, "gid 24369 to devel"),
        ({}, {"ops": 600}, "group ops with gid 600"),
    ],
)
def test_script_refuses_a_clash(
    mac: Mac, users: dict[str, int], groups: dict[str, int], message: str
) -> None:
    db = mac.db()
    mac.edit(users={**db.users, **users}, groups={**db.groups, **groups})
    result = mac.go()
    assert result.returncode == 1
    assert message in result.stderr
    assert "Renumber ops first" in result.stderr
    assert not [call for call in mac.calls() if " -create " in call]
    assert not (mac.root / "etc").exists()


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "\n\n",
        f'command="id" {KEY}\n',
        f"from=host {KEY}\n",
        "ssh-ed25519 !!!\n",
        "# a comment\n",
        f"{KEY}\r\n",
        f"{KEY} fine\n$(touch pwned)\n",
    ],
)
def test_script_rejects_bad_key_files(mac: Mac, contents: str) -> None:
    mac.keys.write_text(contents, encoding="utf-8")
    result = mac.go()
    assert result.returncode == 1
    assert "keys" in result.stderr
    assert mac.calls() == ["id -u"]
    assert not (mac.root / "etc").exists()


def test_script_takes_a_key_file_without_a_final_newline(mac: Mac) -> None:
    mac.keys.write_text(f"{KEY}\n\n{KEY} second", encoding="utf-8")
    assert mac.go().returncode == 0
    assert mac.authorized_keys.read_text() == f"{KEY}\n{KEY} second\n"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "--keys FILE is required"),
        (("--keys",), "--keys needs a file"),
        (("--keys", "/nonexistent"), "is not a readable file"),
        (("--install",), "unknown argument: --install"),
        (("--keys=x",), "unknown argument: --keys=x"),
        (("$(touch pwned)",), "unknown argument"),
    ],
)
def test_script_rejects_bad_arguments(
    mac: Mac, args: tuple[str, ...], message: str
) -> None:
    result = mac.run(*args)
    assert result.returncode == 1
    assert message in result.stderr
    assert mac.calls() == []
    assert not Path("pwned").exists()


def test_script_needs_root_for_a_real_run(mac: Mac) -> None:
    result = mac.run("--keys", str(mac.keys), uid="501")
    assert result.returncode == 1
    assert "run it with sudo" in result.stderr
    assert "ops" not in mac.db().users


def test_script_help(mac: Mac) -> None:
    result = mac.run("--help")
    assert result.returncode == 0
    assert "sudo first-run-macos.sh [--dry-run] --keys FILE" in result.stdout
    assert "set -euo" not in result.stdout
    assert mac.calls() == []
