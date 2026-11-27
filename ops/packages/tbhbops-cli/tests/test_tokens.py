"""Tests for the token grants, the script that creates them and the reach check."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Self, override
lazy from collections.abc import Collection

import pytest
from tbhbops_cli import tokens
from tbhbops_cli.common import CliError

AGENTS = ("ex-ctl1", "ex-ctl2", "ex-lab1", "ex-win1")
CONTROLLERS = ("ex-ctl1", "ex-ctl2")
WHEN = "20261108T142100Z"
ACCOUNT = "acct.example.com"
HERE = Path(__file__).parent
SH = "/bin/sh"
SSH_FLAGS = "-o IdentityAgent=none -o IdentitiesOnly=yes -i"
OPS_VAULTS = ("host-storage-nas-a", "host-storage-nas-b")
NEEDED = ("host-0-shared", *(f"host-{h}" for h in AGENTS), *OPS_VAULTS)


def script(
    hosts: tuple[str, ...] = AGENTS,
    agents: tuple[str, ...] = AGENTS,
    *,
    session: str | None = None,
    runner_vms: tuple[str, ...] = (),
    dry_run: bool = False,
) -> str:
    ops = tokens.select_ops(CONTROLLERS, hosts)
    return tokens.commands(
        "srv",
        hosts,
        agents,
        CONTROLLERS,
        WHEN,
        account=ACCOUNT,
        ops_hosts=ops,
        session_host=session,
        runner_vms=runner_vms,
        dry_run=dry_run,
    )


def test_the_script_matches_its_golden_text() -> None:
    assert script() == (HERE / "golden_token_commands.txt").read_text(encoding="utf-8")


def test_the_dry_run_script_matches_its_golden_text() -> None:
    assert script(dry_run=True) == (
        HERE / "golden_token_commands_dry_run.txt"
    ).read_text(encoding="utf-8")


def test_the_script_starts_with_set_eu_and_says_who_runs_it() -> None:
    text = script()
    assert text.startswith("set -eu\n")
    assert (
        "# @tbhb runs this script once, in their own terminal, and 1Password asks\n"
        "# them to approve once.\n"
    ) in text
    assert "op signin" not in text


def test_no_token_has_an_expiry() -> None:
    assert "expires" not in script()
    assert "expires" not in script(dry_run=True)


def test_an_ordinary_host_reads_its_own_vault_and_host_all() -> None:
    assert tokens.grants("ex-lab1", AGENTS, CONTROLLERS) == (
        tokens.Grant("host-ex-lab1", "r"),
        tokens.Grant("host-0-shared", "r"),
    )


def test_a_controller_writes_host_all_and_reads_every_agent_vault() -> None:
    assert tokens.grants("ex-ctl2", AGENTS, CONTROLLERS) == (
        tokens.Grant("host-0-shared", ""),
        *(tokens.Grant(f"host-{h}", "r") for h in AGENTS),
    )


@pytest.mark.parametrize(
    ("permission", "option"),
    [("", "--vault v"), ("r", "--vault v,r"), ("w", "--vault v,w")],
)
def test_a_vault_option_adds_at_most_one_suffix(permission: str, option: str) -> None:
    assert tokens.vault_option(tokens.Grant("v", permission)) == option


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_vault_argument_has_more_than_one_suffix_and_a_suffix_is_r_or_w(
    *, dry_run: bool
) -> None:
    text = script(dry_run=dry_run)
    found = re.findall(r"--vault ([A-Za-z0-9,_-]+)", text)
    assert found
    for argument in found:
        vault, *suffixes = argument.split(",")
        assert vault.startswith("host-")
        assert len(suffixes) <= 1, argument
        assert suffixes in ([], ["r"], ["w"]), argument
    assert "--vault host-0-shared --vault host-ex-ctl1,r" in text


def test_a_narrowed_script_still_gives_a_controller_every_agent_vault() -> None:
    text = script(("ex-ctl1",))
    assert "# ex-lab1" not in text
    assert "--vault host-0-shared --vault host-ex-ctl1,r --vault host-ex-ctl2,r" in text
    assert "--vault host-ex-win1,r)\n" in text
    for vault in ("host-0-shared", *(f"host-{h}" for h in AGENTS)):
        assert f"vault grant --account {ACCOUNT} --server srv --vault {vault}\n" in text


def test_a_narrowed_script_for_an_ordinary_host_grants_two_vaults() -> None:
    text = script(("ex-lab1",))
    assert text.count("op connect vault grant") == 2
    assert "ex-win1" not in text


def test_the_token_name_has_the_prefix_host_and_stamp() -> None:
    assert (
        f"op connect token create --account {ACCOUNT} "
        "'agent-host:ex-lab1:20261108T142100Z'"
        " --server srv --vault host-ex-lab1,r --vault host-0-shared,r"
    ) in script(("ex-lab1",))


def test_a_token_name_of_the_limit_passes() -> None:
    host = "h" * (tokens.NAME_LIMIT - len("agent-host::20261108T142100Z"))
    name = tokens.token_name("agent-host", host, "20261108T142100Z")
    assert len(name) == tokens.NAME_LIMIT == 64


def test_a_token_name_over_the_limit_stops_the_script() -> None:
    host = "h" * (tokens.NAME_LIMIT + 1 - len("agent-host::20261108T142100Z"))
    with pytest.raises(CliError) as caught:
        tokens.token_name("agent-host", host, "20261108T142100Z")
    assert str(caught.value) == (
        f"the token name agent-host:{host}:20261108T142100Z has 65 characters,"
        " over the 64 Connect accepts"
    )


def blocks(text: str) -> list[str]:
    """TEXT in the chunks that blank lines separate: the header, then one per host."""
    return text.split("\n\n")


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_host_ops_vault_appears_only_in_the_grants_and_ops_controller_blocks(
    *, dry_run: bool
) -> None:
    text = script(dry_run=dry_run)
    found = [b for b in blocks(text) if "host-storage-" in b]
    assert len(found) == 2 + len(CONTROLLERS)
    created, grants, *ops = found
    dry_run_creates = {
        f"echo 'op vault create --account {ACCOUNT} {vault}'" for vault in OPS_VAULTS
    }
    for line in created.splitlines():
        assert "host-storage-" not in line or (
            line in dry_run_creates or line.startswith("for name in ")
        )
    for line in grants.splitlines():
        assert (
            "host-storage-" not in line
            or "vault grant --account acct.example.com --server srv --vault" in line
        )
    assert all(b.startswith("# ops-controller ex-ctl") for b in ops)


@pytest.mark.parametrize("dry_run", [False, True])
def test_an_ops_controller_token_never_reaches_op_item_create(*, dry_run: bool) -> None:
    ops = [
        b for b in blocks(script(dry_run=dry_run)) if b.startswith("# ops-controller ")
    ]
    assert len(ops) == len(CONTROLLERS)
    for block in ops:
        assert "op item" not in block
        assert "jq" not in block


def test_a_script_for_an_agent_host_that_is_no_controller_has_no_ops_token() -> None:
    for dry_run in (False, True):
        text = script(("ex-lab1",), dry_run=dry_run)
        assert "host-storage-" not in text
        assert "ops-controller" not in text
        assert "ssh" not in text


def test_the_ops_controller_token_is_named_and_scoped_to_both_ops_vaults() -> None:
    text = script(("ex-ctl2",))
    assert (
        f"tok=$(op connect token create --account {ACCOUNT} "
        "'ops-controller:ex-ctl2:20261108T142100Z' --server srv "
        "--vault host-storage-nas-a,r --vault host-storage-nas-b,r)\n"
    ) in text
    assert "# ops-controller ex-ctl1" not in text
    for vault in ("host-storage-nas-a", "host-storage-nas-b"):
        assert f"vault grant --account {ACCOUNT} --server srv --vault {vault}\n" in text


def test_the_ops_controller_grants_are_read_only() -> None:
    assert tokens.ops_grants() == (
        tokens.Grant("host-storage-nas-a", "r"),
        tokens.Grant("host-storage-nas-b", "r"),
    )
    assert tokens.ops_controller("ex-ctl1", CONTROLLERS) == tokens.ops_grants()


def test_an_ops_controller_must_be_an_operator_mac() -> None:
    with pytest.raises(CliError) as caught:
        tokens.ops_controller("ex-lab1", CONTROLLERS)
    assert str(caught.value) == "not an operator Mac in the inventory: ex-lab1"


def test_the_ops_hosts_are_the_selected_operator_macs_in_name_order() -> None:
    assert tokens.select_ops(("b", "a", "c"), ("c", "x", "a")) == ("a", "c")
    assert tokens.select_ops(CONTROLLERS, ("ex-lab1",)) == ()


def test_the_header_names_ssh_only_when_an_ops_token_is_made() -> None:
    plain, ops = script(("ex-lab1",)), script(("ex-ctl1",))
    assert "It needs op and jq.\n" in plain
    assert "It needs op, jq and ssh.\n" in ops
    dry_plain, dry_ops = (
        script(("ex-lab1",), dry_run=True),
        script(("ex-ctl1",), dry_run=True),
    )
    assert "# Dry run: each op command below is echoed, not run.\n" in dry_plain
    assert "# Dry run: each op and ssh command below is echoed, not run.\n" in dry_ops


@pytest.mark.parametrize("host", ["storage-nas-a", "storage-nas-b"])
def test_a_host_ops_vault_in_a_grant_is_refused(host: str) -> None:
    with pytest.raises(CliError, match=rf"refusing to grant host-{host}: no token"):
        tokens.grants(host, (*AGENTS, host), CONTROLLERS)
    with pytest.raises(CliError, match="host-storage-"):
        tokens.grants("ex-ctl1", (*AGENTS, host), CONTROLLERS)
    with pytest.raises(CliError, match="host-storage-"):
        script((host,), (*AGENTS, host))


def test_quoting_keeps_a_single_quote_inside_a_quoted_name() -> None:
    assert tokens._quote("a'b") == "'a'\\''b'"
    assert tokens._quote("ab") == "'ab'"


def test_a_server_name_with_a_space_is_quoted() -> None:
    text = tokens.commands(
        "my server",
        ("ex-lab1",),
        AGENTS,
        CONTROLLERS,
        WHEN,
        account=ACCOUNT,
        ops_hosts=(),
        dry_run=False,
    )
    assert "--server 'my server'" in text


def test_an_account_name_with_a_space_is_quoted_on_every_op_command() -> None:
    text = tokens.commands(
        "srv",
        ("ex-ctl1",),
        AGENTS,
        CONTROLLERS,
        WHEN,
        account="my acct",
        ops_hosts=("ex-ctl1",),
        dry_run=False,
    )
    assert text.count("--account 'my acct'") == len(op_commands(text)) == 14
    assert "--account my acct" not in text


def test_the_stamp_is_in_utc() -> None:
    local = datetime(2026, 11, 8, 9, 21, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert tokens.stamp(local) == WHEN
    assert tokens.stamp(datetime(2026, 11, 8, 14, 21, 0, tzinfo=UTC)) == WHEN


class _AsUtcOnly(datetime):
    @override
    def astimezone(self, tz: tzinfo | None = None) -> Self:
        assert tz is UTC
        return super().astimezone(tz)


def test_the_stamp_converts_to_utc_not_the_system_zone() -> None:
    now = _AsUtcOnly(2026, 11, 8, 9, 21, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert tokens.stamp(now) == WHEN


def test_select_takes_every_agent_host_unless_narrowed() -> None:
    assert tokens.select(AGENTS, []) == AGENTS
    assert tokens.select(AGENTS, ["ex-win1", "ex-lab1", "ex-win1"]) == (
        "ex-lab1",
        "ex-win1",
    )


def test_select_refuses_a_host_that_is_not_an_agent_host() -> None:
    with pytest.raises(CliError) as caught:
        tokens.select(AGENTS, ["ex-lab1", "zz", "aa"])
    assert str(caught.value) == "not an agent host in the inventory: aa, zz"


def test_reach_marks_each_vault_ok_missing_or_extra_in_name_order() -> None:
    assert tokens.reach({"b", "c", "x"}, {"a", "b", "c"}) == [
        ("missing", "a"),
        ("ok", "b"),
        ("ok", "c"),
        ("extra", "x"),
    ]
    assert tokens.reach(set(), set()) == []


def test_the_ops_session_script_matches_its_golden_text() -> None:
    assert script(("ex-lab1",), session="ex-ctl1") == (
        HERE / "golden_token_commands_ops_session.txt"
    ).read_text(encoding="utf-8")


def test_the_ops_session_dry_run_script_matches_its_golden_text() -> None:
    assert script(("ex-lab1",), session="ex-ctl1", dry_run=True) == (
        HERE / "golden_token_commands_ops_session_dry_run.txt"
    ).read_text(encoding="utf-8")


def test_the_dry_run_script_prints_each_op_command_when_run() -> None:
    done = subprocess.run(  # the fixed shell, a script on stdin
        [SH], input=script(dry_run=True), capture_output=True, text=True, check=True
    )
    lines = done.stdout.splitlines()
    assert lines[0] == "op vault list --account acct.example.com --format json"
    assert lines[1:3] == [
        "op vault create --account acct.example.com host-0-shared",
        "op vault create --account acct.example.com host-ex-ctl1",
    ]
    assert (
        "op connect vault grant --account acct.example.com --server srv"
        " --vault host-0-shared"
    ) in lines
    assert (
        "tok=$(op connect token create --account acct.example.com"
        " 'agent-host:ex-lab1:20261108T142100Z'"
        " --server srv --vault host-ex-lab1,r --vault host-0-shared,r)"
    ) in lines
    assert (
        "printf '%s' \"$tok\""
        ' | jq -R \'{title: "connect-token", category: "API_CREDENTIAL", '
        'fields: [{label: "token", type: "CONCEALED", value: .}]}\' '
        "| op item create --account acct.example.com --vault host-ex-lab1 -"
    ) in lines
    assert lines.count("unset tok") == len(AGENTS) + len(CONTROLLERS)
    # The vault list, then a create and a grant for each of seven vaults (host-0-shared,
    # four host vaults, two ops vaults), then the lines of four agent-host
    # blocks and two ops-controller blocks.
    assert len(lines) == 1 + 14 + 4 * len(AGENTS) + 7 * len(CONTROLLERS)
    assert (
        "ssh -n -o IdentityAgent=none -o IdentitiesOnly=yes"
        ' -i "$HOME/.ssh/sessions/$(hostname -s)_ed25519"'
        " ops@ex-ctl1 'security find-generic-password"
        " -s ops-connect -a agent ops-connect.keychain'"
    ) in lines


def op_commands(text: str) -> list[str]:
    """Each line of TEXT from the first `op` command on, as op runs it.

    A comment and an echoed message run nothing, so they are skipped. TEXT is a
    live script, or what a dry-run script prints when it runs.
    """
    runs = [
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(("#", "echo"))
    ]
    found = (re.search(r"\bop (?:connect|item|vault) ", line) for line in runs)
    return [
        line[match.start() :] for line, match in zip(runs, found, strict=True) if match
    ]


def printed(text: str) -> str:
    """What the dry-run script TEXT prints when /bin/sh runs it."""
    return subprocess.run(  # the fixed shell, a script on stdin
        [SH], input=text, capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.parametrize("dry_run", [False, True])
def test_every_op_command_names_the_account(*, dry_run: bool) -> None:
    text = script(dry_run=dry_run)
    commands = op_commands(printed(text) if dry_run else text)
    # The vault list and the vault creates (one in the live loop, seven echoed),
    # seven grants, then per agent host a get, a token create and an item create,
    # and per operator Mac a token create and a token delete.
    creates = 7 if dry_run else 1
    assert len(commands) == 1 + creates + 7 + 3 * len(AGENTS) + 2 * len(CONTROLLERS)
    for command in commands:
        assert f" --account {ACCOUNT} " in command, command


FAKE_TOKEN = {
    "ok": "echo secret-token",
    "longest": f"echo {'a' * tokens.TOKEN_MAX}",
    "over": f"echo {'a' * (tokens.TOKEN_MAX + 1)}",
    "empty": "exit 0",
}
FAKE_LIST = {
    "fail": "echo 'op: no session' >&2; exit 1",
    "garbage": "echo 'not json'",
}
# The jq of the fakes: -R copies stdin, as for the token. Otherwise it answers the
# vault query: whether the list on stdin holds a vault named after --arg.
# Resolve the Bazel runfiles path to stay below Linux's shebang length limit.
FAKE_JQ = f"""#!{Path(sys.executable).resolve()}
import json
import sys

args = sys.argv[1:]
if args[0] == "-R":
    sys.stdout.write(sys.stdin.read())
else:
    name = args[args.index("--arg") + 2]
    print("true" if any(v["name"] == name for v in json.load(sys.stdin)) else "false")
"""


def vault_list(vaults: Collection[str]) -> str:
    """The JSON that `op vault list --format json` prints for VAULTS."""
    return json.dumps([{"id": f"id-{v}", "name": v} for v in vaults])


def fake_bin(
    root: Path,
    *,
    held: Collection[str] = (),
    vaults: Collection[str] = NEEDED,
    listing: str = "ok",
) -> Path:
    """A PATH directory whose op, jq and ssh are fakes that log what they got.

    VAULTS are the vaults the account has. LISTING is `ok`, or how `op vault
    list` goes wrong.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log = root / "argv.log"
    body = root / "stdin.log"
    held_cases = "".join(f'      *"--vault {v}") exit 0 ;;\n' for v in held)
    shown = FAKE_LIST.get(listing, f"echo '{vault_list(vaults)}'")
    op = f"""#!/bin/sh
echo "$*" >> {log}
case "$1 $2" in
  "vault list") {shown} ;;
  "item get")
    case "$*" in
{held_cases}    esac
    exit 1 ;;
  "connect token") echo secret-token ;;
  "item create") cat > {body} ;;
esac
"""
    (bin_dir / "op").write_text(op, encoding="utf-8")
    (bin_dir / "jq").write_text(FAKE_JQ, encoding="utf-8")
    # An operator Mac's keychain search finds the item, so its block is skipped.
    ssh = f"""#!/bin/sh
echo "ssh $*" >> {log}
case "$*" in
  *"security -i"*|*"sh -s"*) cat > /dev/null ;;
esac
"""
    (bin_dir / "ssh").write_text(ssh, encoding="utf-8")
    for name in ("op", "jq", "ssh"):
        (bin_dir / name).chmod(stat.S_IRWXU)
    return bin_dir


def run_live(  # the fake's knobs, each one a fact
    root: Path,
    *,
    held: Collection[str] = (),
    hosts: tuple[str, ...] = ("ex-lab1",),
    runner_vms: tuple[str, ...] = (),
    vaults: Collection[str] = NEEDED,
    listing: str = "ok",
) -> subprocess.CompletedProcess[str]:
    bin_dir = fake_bin(root, held=held, vaults=vaults, listing=listing)
    env = sh_env(root, bin_dir)
    return subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input=script(hosts, runner_vms=runner_vms),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_the_token_goes_from_create_to_item_create_and_never_into_argv(
    tmp_path: Path,
) -> None:
    done = run_live(tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "secret-token" not in argv
    assert argv.splitlines()[-1] == (
        "item create --account acct.example.com --vault host-ex-lab1 -"
    )
    assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == "secret-token"


def test_a_host_that_already_holds_the_item_is_skipped_before_any_token(
    tmp_path: Path,
) -> None:
    done = run_live(tmp_path, held=["host-ex-lab1"])
    assert (done.returncode, done.stdout) == (0, "")
    assert done.stderr == "host-ex-lab1 already holds connect-token; skipping\n"
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "token create" not in argv
    assert "item create" not in argv
    assert "secret-token" not in done.stdout + done.stderr


def test_a_skipped_host_does_not_stop_the_hosts_after_it(tmp_path: Path) -> None:
    done = run_live(tmp_path, held=["host-ex-lab1"], hosts=("ex-lab1", "ex-win1"))
    assert (done.returncode, done.stdout) == (0, "")
    assert done.stderr == "host-ex-lab1 already holds connect-token; skipping\n"
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines()
    assert [a for a in argv if a.startswith("connect token create")] == [
        (
            "connect token create --account acct.example.com"
            " agent-host:ex-win1:20261108T142100Z"
            " --server srv --vault host-ex-win1,r --vault host-0-shared,r"
        )
    ]
    assert argv[-1] == "item create --account acct.example.com --vault host-ex-win1 -"
    assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == "secret-token"


def sh_env(root: Path, bin_dir: Path) -> dict[str, str]:
    """The environment of a script run: fake commands, and HOME under ROOT."""
    hostname = bin_dir / "hostname"
    hostname.write_text(
        '#!/bin/sh\n[ "$1" = "-s" ] || exit 1\necho ex-controller\n', "utf-8"
    )
    hostname.chmod(stat.S_IRWXU)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(root)}


def fake_ops_bin(
    root: Path, *, look: tuple[int, int], create: str, store: int, delete: int = 0
) -> Path:
    """A PATH directory whose op and ssh are fakes that log what they got.

    LOOK is the exit status of the first keychain search and of the second.
    DELETE is the exit status of `op connect token delete`.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir()
    op = f"""#!/bin/sh
echo "op $*" >> {root}/argv.log
case "$1 $2" in
  "connect token")
    case "$3" in
      delete) exit {delete} ;;
      *) {FAKE_TOKEN[create]} ;;
    esac ;;
esac
"""
    ssh = f"""#!/bin/sh
echo "ssh $*" >> {root}/argv.log
case "$*" in
  *"security -i"*) cat > {root}/stdin.log; exit {store} ;;
  *find-generic-password*)
    echo x >> {root}/looks.log
    if [ "$(wc -l < {root}/looks.log)" -eq 1 ]; then exit {look[0]}; fi
    exit {look[1]} ;;
  *"sh -s"*) cat > {root}/prepare.log ;;
esac
"""
    for name, body in (("op", op), ("ssh", ssh)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(stat.S_IRWXU)
    return bin_dir


DELETE_ARGV = (
    "op connect token delete --account acct.example.com"
    " ops-controller:ex-ctl1:20261108T142100Z --server srv"
)
DELETED = (
    "deleted the ops-controller token ops-controller:ex-ctl1:20261108T142100Z"
    " from srv\n"
)


def run_ops(
    root: Path,
    *,
    look: tuple[int, int] = (tokens.NOT_FOUND, 0),
    create: str = "ok",
    store: int = 0,
    delete: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run only ex-ctl1's ops-controller block, against fake op and ssh."""
    bin_dir = fake_ops_bin(root, look=look, create=create, store=store, delete=delete)
    text = script(("ex-ctl1",))
    return subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input="set -eu\n" + text[text.index("# ops-controller ex-ctl1") :],
        capture_output=True,
        text=True,
        check=False,
        env=sh_env(bin_dir.parent, bin_dir),
    )


def test_the_ops_token_goes_to_security_on_stdin_and_never_into_argv(
    tmp_path: Path,
) -> None:
    done = run_ops(tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "secret-token" not in argv
    assert "item" not in argv
    assert argv.count("find-generic-password") == 2
    assert "token delete" not in argv
    assert argv.splitlines()[-2] == (
        f"ssh {SSH_FLAGS} {tmp_path}/.ssh/sessions/ex-controller_ed25519 ops@ex-ctl1"
        ' security unlock-keychain -p "" ops-connect.keychain && security -i'
    )
    assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == (
        "add-generic-password -U -s ops-connect -a agent -T /usr/bin/security"
        " -w secret-token ops-connect.keychain\n"
    )
    assert "create-keychain" in (tmp_path / "prepare.log").read_text(encoding="utf-8")


def test_an_ops_keychain_that_already_holds_the_item_is_skipped_before_any_token(
    tmp_path: Path,
) -> None:
    done = run_ops(tmp_path, look=(0, 0))
    assert (done.returncode, done.stdout) == (0, "")
    assert done.stderr == "ops on ex-ctl1 already holds ops-connect; skipping\n"
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "token create" not in argv
    assert "security -i" not in argv
    assert not (tmp_path / "stdin.log").exists()


def test_every_ssh_runs_as_ops_with_the_controller_session_key_and_no_sudo(
    tmp_path: Path,
) -> None:
    done = run_ops(tmp_path)
    assert done.returncode == 0
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "sudo" not in argv
    ssh = [line for line in argv.splitlines() if line.startswith("ssh ")]
    assert len(ssh) == 4
    for line in ssh:
        assert (
            line.removeprefix("ssh -n ")
            .removeprefix("ssh ")
            .startswith(
                f"{SSH_FLAGS} {tmp_path}/.ssh/sessions/ex-controller_ed25519"
                " ops@ex-ctl1 "
            )
        ), line


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_script_never_uses_sudo_and_every_ssh_names_the_controller_session_key(
    *, dry_run: bool
) -> None:
    text = script(dry_run=dry_run)
    assert "sudo" not in text
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    found = [m for line in lines for m in re.findall(r"\bssh (.*)", line)]
    assert len(found) == 4 * len(CONTROLLERS)
    for rest in found:
        assert re.match(
            rf"(-n )?{re.escape(SSH_FLAGS)} "
            + re.escape('"$HOME/.ssh/sessions/$(hostname -s)_ed25519"')
            + r" ops@ex-ctl[12] ",
            rest,
        ), rest


@pytest.mark.parametrize("controller", ["mac-a", "mac-b"])
def test_the_operator_shell_expands_the_session_key_at_run_time(
    tmp_path: Path, controller: str
) -> None:
    # A space in HOME must remain inside the single identity argument.
    root = tmp_path / "operator home"
    root.mkdir()
    bin_dir = root / "bin"
    bin_dir.mkdir()
    env = sh_env(root, bin_dir)
    (bin_dir / "hostname").write_text(
        f'#!/bin/sh\n[ "$1" = "-s" ] || exit 1\necho {controller}\n', "utf-8"
    )
    fake_ssh = bin_dir / "ssh"
    fake_ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_ssh.chmod(stat.S_IRWXU)
    command = tokens._ssh("ex-ctl1", "true")
    done = subprocess.run(
        [SH], input=command, capture_output=True, text=True, check=True, env=env
    )
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "-o",
        "IdentityAgent=none",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(root / ".ssh" / "sessions" / f"{controller}_ed25519"),
        "ops@ex-ctl1",
        "true",
    ]


def test_a_rerun_after_everything_is_made_skips_every_host_and_exits_zero(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        (
            "op",
            (
                f'#!/bin/sh\necho "op $*" >> {tmp_path}/argv.log\n'
                f'case "$1 $2" in "vault list") echo \'{vault_list(NEEDED)}\' ;; esac\n'
            ),
        ),
        ("jq", FAKE_JQ),
        (
            "ssh",
            (
                f'#!/bin/sh\necho "ssh $*" >> {tmp_path}/argv.log\n'
                'case "$*" in "-n "*) ;; *) cat >/dev/null ;; esac\n'
            ),
        ),
    ):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(stat.S_IRWXU)
    done = subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input=script(("ex-ctl1", "ex-lab1")),
        capture_output=True,
        text=True,
        check=False,
        env=sh_env(tmp_path, bin_dir),
    )
    assert (done.returncode, done.stdout) == (0, "")
    assert done.stderr == (
        "host-ex-ctl1 already holds connect-token; skipping\n"
        "host-ex-lab1 already holds connect-token; skipping\n"
        "ops on ex-ctl1 already holds ops-connect; skipping\n"
    )
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "token create" not in argv
    assert "item create" not in argv
    assert "security -i" not in argv
    assert "vault create" not in argv


def test_a_failed_keychain_search_is_not_taken_for_an_absent_item(
    tmp_path: Path,
) -> None:
    done = run_ops(tmp_path, look=(255, 0))
    assert done.returncode == 1
    assert done.stderr == "could not look in the ops keychain on ex-ctl1\n"
    assert "token create" not in (tmp_path / "argv.log").read_text(encoding="utf-8")


def test_an_empty_ops_token_stops_the_script_before_security(tmp_path: Path) -> None:
    done = run_ops(tmp_path, create="empty")
    assert done.returncode == 1
    assert done.stderr == (
        "op connect token create printed no token for ops-controller ex-ctl1\n"
    )
    assert not (tmp_path / "stdin.log").exists()


def test_an_ops_token_the_keychain_line_cannot_carry_is_never_sent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "a"
    root.mkdir()
    bin_dir = fake_ops_bin(root, look=(tokens.NOT_FOUND, 0), create="ok", store=0)
    (bin_dir / "op").write_text("#!/bin/sh\necho 'bad token'\n", encoding="utf-8")
    text = script(("ex-ctl1",))
    done = subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input="set -eu\n" + text[text.index("# ops-controller ex-ctl1") :],
        capture_output=True,
        text=True,
        check=False,
        env=sh_env(bin_dir.parent, bin_dir),
    )
    assert done.returncode == 1
    assert done.stderr == (
        "the ops-controller token for ex-ctl1 has a character the keychain line"
        " cannot carry\n"
    )
    assert not (root / "stdin.log").exists()


def test_the_token_longest_the_keychain_line_carries_is_stored(
    tmp_path: Path,
) -> None:
    done = run_ops(tmp_path, create="longest")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    stored = (tmp_path / "stdin.log").read_text(encoding="utf-8")
    assert len(stored) - 1 == tokens.LINE_MAX
    assert "token delete" not in (tmp_path / "argv.log").read_text(encoding="utf-8")


def test_the_token_max_leaves_room_for_the_rest_of_the_keychain_line() -> None:
    rest = tokens.STORE_LINE.replace("$tok", "")
    assert tokens.LINE_MAX == 4095
    assert tokens.TOKEN_MAX + len(rest) == tokens.LINE_MAX


def test_an_over_length_ops_token_is_deleted_and_never_sent_to_security(
    tmp_path: Path,
) -> None:
    done = run_ops(tmp_path, create="over")
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == (
        f"the ops-controller token for ex-ctl1 has {tokens.TOKEN_MAX + 1} characters,"
        f" over the {tokens.TOKEN_MAX} the keychain line can carry\n" + DELETED
    )
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert argv.count("op connect token delete") == 1
    assert argv.splitlines()[-1] == DELETE_ARGV
    assert "security -i" not in argv
    assert "a" * 20 not in argv
    assert not (tmp_path / "stdin.log").exists()


def test_a_failed_store_deletes_the_token_it_made(tmp_path: Path) -> None:
    done = run_ops(tmp_path, store=1)
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == (
        "the ops-controller token for ex-ctl1 was made but not stored\n" + DELETED
    )
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert argv.splitlines()[-1] == DELETE_ARGV
    assert "secret-token" not in argv
    assert (tmp_path / "stdin.log").exists()


@pytest.mark.parametrize(("create", "store"), [("over", 0), ("ok", 1)])
def test_a_token_that_cannot_be_deleted_is_left_for_deleting_by_hand(
    tmp_path: Path, create: str, store: int
) -> None:
    done = run_ops(tmp_path, create=create, store=store, delete=1)
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr.endswith(
        "could not delete the ops-controller token"
        " ops-controller:ex-ctl1:20261108T142100Z;"
        " delete it by hand with op connect token delete\n"
    )
    assert "deleted the" not in done.stderr
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert argv.splitlines()[-1] == DELETE_ARGV


def test_a_keychain_that_lacks_the_item_after_the_store_fails_the_script(
    tmp_path: Path,
) -> None:
    done = run_ops(tmp_path, look=(tokens.NOT_FOUND, tokens.NOT_FOUND))
    assert done.returncode == 1
    assert done.stderr == "the ops keychain on ex-ctl1 does not hold the token\n"


def test_a_failed_keychain_preparation_stops_the_script_first(tmp_path: Path) -> None:
    bin_dir = fake_ops_bin(tmp_path, look=(tokens.NOT_FOUND, 0), create="ok", store=0)
    (bin_dir / "ssh").write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n", "utf-8")
    text = script(("ex-ctl1",))
    done = subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input="set -eu\n" + text[text.index("# ops-controller ex-ctl1") :],
        capture_output=True,
        text=True,
        check=False,
        env=sh_env(bin_dir.parent, bin_dir),
    )
    assert done.returncode == 1
    assert done.stderr == "could not prepare the ops keychain on ex-ctl1\n"
    assert not (tmp_path / "argv.log").exists()


def test_the_vaults_are_listed_once_and_created_before_the_first_grant() -> None:
    text = script()
    assert text.count("op vault list") == 1
    assert text.index("op vault list") < text.index("op vault create")
    assert text.index("op vault create") < text.index("op connect vault grant")
    names = re.search(r"^for name in (.*); do$", text, re.MULTILINE)
    assert names
    assert names.group(1).split() == list(NEEDED)


def test_a_script_for_an_ordinary_host_creates_only_its_own_vaults() -> None:
    text = script(("ex-lab1",))
    names = re.search(r"^for name in (.*); do$", text, re.MULTILINE)
    assert names
    assert names.group(1) == "host-0-shared host-ex-lab1"


def test_a_vault_name_with_a_space_is_quoted_in_the_vault_loop() -> None:
    text = tokens.commands(
        "srv",
        ("a b",),
        ("a b",),
        (),
        WHEN,
        account=ACCOUNT,
        ops_hosts=(),
        dry_run=False,
    )
    assert "for name in host-0-shared 'host-a b'; do\n" in text
    dry = tokens.commands(
        "srv",
        ("a b",),
        ("a b",),
        (),
        WHEN,
        account=ACCOUNT,
        ops_hosts=(),
        dry_run=True,
    )
    assert printed(dry).count(f"op vault create --account {ACCOUNT} 'host-a b'\n") == 1


SKIPPED = "".join(
    f"ops on {h} already holds ops-connect; skipping\n" for h in CONTROLLERS
)


def creates(root: Path) -> list[str]:
    """The names given to `op vault create`, in the order of the calls."""
    argv = (root / "argv.log").read_text(encoding="utf-8").splitlines()
    return [a.split()[-1] for a in argv if a.startswith("vault create ")]


def test_with_every_vault_missing_each_is_created_once_before_the_first_grant(
    tmp_path: Path,
) -> None:
    done = run_live(tmp_path, vaults=(), hosts=AGENTS)
    assert (done.returncode, done.stderr) == (0, SKIPPED)
    assert done.stdout == "".join(f"created vault {v}\n" for v in NEEDED)
    assert creates(tmp_path) == list(NEEDED)
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines()
    assert argv[0] == f"vault list --account {ACCOUNT} --format json"
    assert argv[1:8] == [f"vault create --account {ACCOUNT} {v}" for v in NEEDED]
    assert argv[8].startswith("connect vault grant ")
    assert [i for i, a in enumerate(argv) if a.startswith("vault ")] == list(range(8))


def test_with_every_vault_present_none_is_created_and_nothing_is_printed(
    tmp_path: Path,
) -> None:
    done = run_live(tmp_path, hosts=AGENTS)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", SKIPPED)
    assert creates(tmp_path) == []
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines()
    assert sum(a.startswith("vault list ") for a in argv) == 1


def test_with_some_vaults_missing_only_those_are_created(tmp_path: Path) -> None:
    present = ("host-0-shared", "host-ex-ctl2", "host-ex-win1", "host-storage-nas-a")
    done = run_live(tmp_path, vaults=present, hosts=AGENTS)
    assert (done.returncode, done.stderr) == (0, SKIPPED)
    assert done.stdout == (
        "created vault host-ex-ctl1\n"
        "created vault host-ex-lab1\n"
        "created vault host-storage-nas-b\n"
    )
    assert creates(tmp_path) == ["host-ex-ctl1", "host-ex-lab1", "host-storage-nas-b"]


def test_a_vault_name_must_match_exactly(tmp_path: Path) -> None:
    near = ("host-0-shared-2", "prefix-host-0-shared", "host-ex-lab1 ")
    done = run_live(tmp_path, vaults=near)
    assert (done.returncode, done.stderr) == (0, "")
    assert creates(tmp_path) == ["host-0-shared", "host-ex-lab1"]


def test_a_failed_vault_list_stops_before_any_create_or_grant(tmp_path: Path) -> None:
    done = run_live(tmp_path, listing="fail")
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (
        "op: no session\ncould not list the vaults in the 1Password account\n"
    )
    assert (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines() == [
        f"vault list --account {ACCOUNT} --format json"
    ]


def test_a_vault_list_that_is_not_json_creates_nothing(tmp_path: Path) -> None:
    done = run_live(tmp_path, listing="garbage")
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.endswith("could not read the vault list\n")
    assert creates(tmp_path) == []
    assert "grant" not in (tmp_path / "argv.log").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs the real jq")
@pytest.mark.parametrize(
    ("vaults", "name", "answer"),
    [
        (["a", "b"], "a", "true"),
        (["a", "b"], "ab", "false"),
        (["a-b"], "a", "false"),
        ([], "a", "false"),
    ],
)
def test_the_vault_query_answers_for_an_exact_name_in_the_real_jq(
    vaults: list[str], name: str, answer: str
) -> None:
    done = subprocess.run(  # jq, found on PATH, with fixed arguments
        ["jq", "-r", "--arg", "name", name, tokens.VAULT_JQ],
        input=vault_list(vaults),
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == answer + "\n"


SESSION_NAME = "ops-session:ex-ctl1:20261108T142100Z"
SESSION_DELETE_ARGV = (
    f"op connect token delete --account acct.example.com {SESSION_NAME} --server srv"
)
SESSION_DELETED = f"deleted the ops-session token {SESSION_NAME} from srv\n"


def test_the_ops_session_grants_are_an_operator_macs_and_never_a_host_ops_vault() -> (
    None
):
    found = tokens.session_grants("ex-ctl2", AGENTS, CONTROLLERS)
    assert found == (
        *tokens.grants("ex-ctl2", AGENTS, CONTROLLERS),
        tokens.Grant("overlay-oauth", tokens.READ),
    )
    assert found[0] == tokens.Grant("host-0-shared", tokens.READ_WRITE)
    assert {g.vault for g in found[1:]} == {
        *(f"host-{a}" for a in AGENTS),
        "overlay-oauth",
    }
    assert all(g.permission == tokens.READ for g in found[1:])
    assert not any(g.vault.startswith(tokens.FORBIDDEN) for g in found)


def test_only_the_ops_session_token_reaches_the_tailnet_oauth_vault() -> None:
    assert tokens.VAULT_OAUTH == "overlay-oauth"
    for host in AGENTS:
        assert tokens.VAULT_OAUTH not in {
            g.vault for g in tokens.grants(host, AGENTS, CONTROLLERS)
        }
    assert tokens.VAULT_OAUTH not in {g.vault for g in tokens.ops_grants()}
    plain = script()
    assert "overlay-oauth" not in plain
    text = script(("ex-lab1",), session="ex-ctl1")
    assert text.count("overlay-oauth") >= 4
    assert "op vault create" in text
    assert "--vault overlay-oauth,r)" in text
    assert "--server srv --vault overlay-oauth\n" in text
    assert "for name in host-0-shared host-ex-ctl1 host-ex-ctl2 host-ex-lab1" in text
    assert " overlay-oauth; do" in text


def test_the_ops_session_host_must_be_an_operator_mac() -> None:
    with pytest.raises(CliError) as caught:
        tokens.session_grants("ex-lab1", AGENTS, CONTROLLERS)
    assert str(caught.value) == "not an operator Mac in the inventory: ex-lab1"
    with pytest.raises(CliError):
        script(("ex-lab1",), session="ex-lab1")


def test_the_ops_session_token_is_named_and_scoped_like_an_operator_macs() -> None:
    text = script(("ex-lab1",), session="ex-ctl1")
    assert (
        f"tok=$(op connect token create --account {ACCOUNT} '{SESSION_NAME}'"
        " --server srv --vault host-0-shared --vault host-ex-ctl1,r"
        " --vault host-ex-ctl2,r --vault host-ex-lab1,r --vault host-ex-win1,r"
        " --vault overlay-oauth,r)"
    ) in text
    assert tokens.SESSION_PREFIX == "ops-session"
    assert "host-ops" not in text


def test_the_ops_session_grants_its_vaults_even_when_the_hosts_are_narrowed() -> None:
    text = script(("ex-lab1",), session="ex-ctl1")
    for vault in ("host-ex-ctl1", "host-ex-ctl2", "host-ex-win1"):
        assert f"--server srv --vault {vault}\n" in text


def test_the_ops_session_needs_no_ssh_and_no_other_host_ops_vault() -> None:
    text = script(("ex-lab1",), session="ex-ctl1")
    assert not [x for x in text.splitlines() if "ssh" in x and x[0] != "#"]
    assert "sudo" not in text
    assert "# ops-controller" not in text


def test_the_header_names_the_session_tools_and_the_session() -> None:
    plain = script(("ex-lab1",)).splitlines()
    live = script(("ex-lab1",), session="ex-ctl1").splitlines()
    assert "# An agent only prints it. It needs op, jq and security." in live
    assert "# An agent only prints it. It needs op and jq." in plain
    assert "# ex-ctl1 also gets an ops-session token, which reads every" in live
    assert not any("ops-session" in line for line in plain)
    both = script(("ex-ctl1",), session="ex-ctl1").splitlines()
    assert "# An agent only prints it. It needs op, jq, ssh and security." in both
    dry = script(("ex-lab1",), session="ex-ctl1", dry_run=True).splitlines()
    assert "# Dry run: each op and security command below is echoed, not run." in dry
    both_dry = script(("ex-ctl1",), session="ex-ctl1", dry_run=True).splitlines()
    assert (
        "# Dry run: each op, ssh and security command below is echoed, not run."
    ) in both_dry


def fake_session_bin(
    root: Path, *, look: tuple[int, int], create: str, store: int, delete: int = 0
) -> Path:
    """A PATH directory whose op and security are fakes that log what they got."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    op = f"""#!/bin/sh
echo "op $*" >> {root}/argv.log
case "$1 $2" in
  "connect token")
    case "$3" in
      delete) exit {delete} ;;
      *) {FAKE_TOKEN[create]} ;;
    esac ;;
esac
"""
    security = f"""#!/bin/sh
echo "security $*" >> {root}/argv.log
case "$1" in
  -i) cat > {root}/stdin.log; exit {store} ;;
  find-generic-password)
    echo x >> {root}/looks.log
    if [ "$(wc -l < {root}/looks.log)" -eq 1 ]; then exit {look[0]}; fi
    exit {look[1]} ;;
esac
"""
    for name, body in (("op", op), ("security", security)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(stat.S_IRWXU)
    return bin_dir


def run_session(
    root: Path,
    *,
    look: tuple[int, int] = (tokens.NOT_FOUND, 0),
    create: str = "ok",
    store: int = 0,
    delete: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run only ex-ctl1's ops-session block, against fake op and security."""
    bin_dir = fake_session_bin(
        root, look=look, create=create, store=store, delete=delete
    )
    text = script(("ex-lab1",), session="ex-ctl1")
    return subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input="set -eu\n" + text[text.index("# ops-session ex-ctl1") :],
        capture_output=True,
        text=True,
        check=False,
        env=sh_env(bin_dir.parent, bin_dir),
    )


def test_the_session_token_goes_to_security_on_stdin_and_never_into_argv(
    tmp_path: Path,
) -> None:
    done = run_session(tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "secret-token" not in argv
    assert "ssh" not in argv
    assert "token delete" not in argv
    lines = argv.splitlines()
    assert argv.count("find-generic-password") == 2
    assert lines[:2] == [
        "security create-keychain -p  ops-connect.keychain",
        "security unlock-keychain -p  ops-connect.keychain",
    ]
    assert lines[-3:] == [
        "security unlock-keychain -p  ops-connect.keychain",
        "security -i",
        "security find-generic-password -s ops-connect -a agent ops-connect.keychain",
    ]
    assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == (
        "add-generic-password -U -s ops-connect -a agent -T /usr/bin/security"
        " -w secret-token ops-connect.keychain\n"
    )


def test_a_keychain_that_already_holds_the_session_item_is_skipped_first(
    tmp_path: Path,
) -> None:
    done = run_session(tmp_path, look=(0, 0))
    assert (done.returncode, done.stdout) == (0, "")
    assert done.stderr == (
        "your keychain on ex-ctl1 already holds ops-connect; skipping\n"
    )
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "token create" not in argv
    assert "security -i" not in argv
    assert not (tmp_path / "stdin.log").exists()


def test_a_failed_session_keychain_search_is_not_taken_for_an_absent_item(
    tmp_path: Path,
) -> None:
    done = run_session(tmp_path, look=(255, 0))
    assert done.returncode == 1
    assert done.stderr == "could not look in your keychain on ex-ctl1\n"
    assert "token create" not in (tmp_path / "argv.log").read_text(encoding="utf-8")


def test_an_empty_session_token_stops_the_script_before_security(
    tmp_path: Path,
) -> None:
    done = run_session(tmp_path, create="empty")
    assert done.returncode == 1
    assert done.stderr == (
        "op connect token create printed no token for ops-session ex-ctl1\n"
    )
    assert not (tmp_path / "stdin.log").exists()


def test_a_session_token_the_keychain_line_cannot_carry_is_never_sent(
    tmp_path: Path,
) -> None:
    bin_dir = fake_session_bin(
        tmp_path, look=(tokens.NOT_FOUND, 0), create="ok", store=0
    )
    (bin_dir / "op").write_text("#!/bin/sh\necho 'bad token'\n", encoding="utf-8")
    text = script(("ex-lab1",), session="ex-ctl1")
    done = subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input="set -eu\n" + text[text.index("# ops-session ex-ctl1") :],
        capture_output=True,
        text=True,
        check=False,
        env=sh_env(tmp_path, bin_dir),
    )
    assert done.returncode == 1
    assert done.stderr == (
        "the ops-session token for ex-ctl1 has a character the keychain line"
        " cannot carry\n"
    )
    assert not (tmp_path / "stdin.log").exists()


def test_the_session_token_longest_the_keychain_line_carries_is_stored(
    tmp_path: Path,
) -> None:
    done = run_session(tmp_path, create="longest")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    stored = (tmp_path / "stdin.log").read_text(encoding="utf-8")
    assert len(stored) - 1 == tokens.LINE_MAX
    assert "token delete" not in (tmp_path / "argv.log").read_text(encoding="utf-8")


def test_an_over_length_session_token_is_deleted_and_never_sent_to_security(
    tmp_path: Path,
) -> None:
    done = run_session(tmp_path, create="over")
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == (
        f"the ops-session token for ex-ctl1 has {tokens.TOKEN_MAX + 1} characters,"
        f" over the {tokens.TOKEN_MAX} the keychain line can carry\n" + SESSION_DELETED
    )
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert argv.count("op connect token delete") == 1
    assert argv.splitlines()[-1] == SESSION_DELETE_ARGV
    assert "security -i" not in argv
    assert "a" * 20 not in argv
    assert not (tmp_path / "stdin.log").exists()


def test_a_failed_session_store_deletes_the_token_it_made(tmp_path: Path) -> None:
    done = run_session(tmp_path, store=1)
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == (
        "the ops-session token for ex-ctl1 was made but not stored\n" + SESSION_DELETED
    )
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert argv.splitlines()[-1] == SESSION_DELETE_ARGV
    assert "secret-token" not in argv
    assert (tmp_path / "stdin.log").exists()


@pytest.mark.parametrize(("create", "store"), [("over", 0), ("ok", 1)])
def test_a_session_token_that_cannot_be_deleted_is_left_for_deleting_by_hand(
    tmp_path: Path, create: str, store: int
) -> None:
    done = run_session(tmp_path, create=create, store=store, delete=1)
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr.endswith(
        f"could not delete the ops-session token {SESSION_NAME};"
        " delete it by hand with op connect token delete\n"
    )
    assert "deleted the" not in done.stderr
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert argv.splitlines()[-1] == SESSION_DELETE_ARGV


def test_a_session_keychain_that_lacks_the_item_after_the_store_fails_the_script(
    tmp_path: Path,
) -> None:
    done = run_session(tmp_path, look=(tokens.NOT_FOUND, tokens.NOT_FOUND))
    assert done.returncode == 1
    assert done.stderr == "your keychain on ex-ctl1 does not hold the token\n"


def test_a_failed_session_keychain_preparation_stops_the_script_first(
    tmp_path: Path,
) -> None:
    bin_dir = fake_session_bin(
        tmp_path, look=(tokens.NOT_FOUND, 0), create="ok", store=0
    )
    (bin_dir / "security").write_text("#!/bin/sh\nexit 1\n", "utf-8")
    text = script(("ex-lab1",), session="ex-ctl1")
    done = subprocess.run(  # the fixed shell, a script on stdin
        [SH],
        input="set -eu\n" + text[text.index("# ops-session ex-ctl1") :],
        capture_output=True,
        text=True,
        check=False,
        env=sh_env(tmp_path, bin_dir),
    )
    assert done.returncode == 1
    assert done.stderr == "could not prepare your keychain on ex-ctl1\n"
    assert not (tmp_path / "argv.log").exists()


def test_the_ops_session_dry_run_echoes_each_command_and_runs_none() -> None:
    text = script(("ex-lab1",), session="ex-ctl1", dry_run=True)
    done = subprocess.run(  # the fixed shell, a script on stdin
        [SH], input=text, capture_output=True, text=True, check=True
    )
    lines = done.stdout.splitlines()
    assert done.stderr == ""
    assert (
        "security find-generic-password -s ops-connect -a agent ops-connect.keychain"
        in lines
    )
    assert lines.count("unset tok") == 2


RUNNER_VM = "ex-lab1-ci"
CI_VMS = (RUNNER_VM, "ex-ctl1-ci")


def runner_script(*, dry_run: bool = False) -> str:
    """The script for a runner VM alone, which makes only its token."""
    return script((), runner_vms=(RUNNER_VM,), dry_run=dry_run)


def mixed_script(*, dry_run: bool = False) -> str:
    """The script for an agent host and a runner VM, which makes both kinds."""
    return script(("ex-lab1",), runner_vms=(RUNNER_VM,), dry_run=dry_run)


def test_the_runner_vm_script_matches_its_golden_text() -> None:
    assert runner_script() == (HERE / "golden_token_commands_runner_vm.txt").read_text(
        encoding="utf-8"
    )


def test_the_runner_vm_dry_run_script_matches_its_golden_text() -> None:
    assert runner_script(dry_run=True) == (
        HERE / "golden_token_commands_runner_vm_dry_run.txt"
    ).read_text(encoding="utf-8")


def test_the_mixed_runner_vm_script_matches_its_golden_text() -> None:
    assert mixed_script() == (
        HERE / "golden_token_commands_runner_vm_with_host.txt"
    ).read_text(encoding="utf-8")


def test_a_runner_vm_alone_makes_no_host_section_and_no_host_all() -> None:
    for text in (runner_script(), runner_script(dry_run=True)):
        assert "host-0-shared" not in text
        assert "agent-host" not in text
        assert "connect-token\n" not in text
        assert "ops-controller" not in text
        assert "ex-lab1-ci" in text


def test_a_runner_vm_with_a_host_makes_both_kinds() -> None:
    text = mixed_script()
    assert "agent-host:ex-lab1:" in text
    assert "runner-vm:ex-lab1-ci:" in text
    assert "--vault host-0-shared\n" in text


def test_a_runner_vm_with_a_session_makes_every_host_and_the_session() -> None:
    text = script((), runner_vms=(RUNNER_VM,), session="ex-ctl1")
    assert "# ops-session ex-ctl1\n" in text
    assert "runner-vm:ex-lab1-ci:" in text
    assert "--vault host-0-shared\n" in text


def test_a_script_without_runner_vms_never_names_the_runner_item() -> None:
    assert "runner-access-token" not in script()
    assert "runner-fixture" not in script()
    assert "runner-vm" not in script()


def test_a_runner_vm_token_reads_only_github_runners_read_only() -> None:
    assert tokens.runner_grants() == (tokens.Grant("runner-fixture", "r"),)
    text = runner_script()
    create = [
        line
        for line in text.splitlines()
        if "runner-vm:ex-lab1-ci" in line and "token create" in line
    ]
    assert create == [
        (
            "  tok=$(op connect token create --account acct.example.com"
            " 'runner-vm:ex-lab1-ci:20261108T142100Z' --server srv"
            " --vault runner-fixture,r)"
        )
    ]
    assert "host-ops" not in "".join(create)


def test_the_server_grants_github_runners_and_the_lab_nodes_vault() -> None:
    text = runner_script()
    assert "--vault runner-fixture\n" in text
    only = script((), runner_vms=(RUNNER_VM,))
    assert "op connect vault grant --account acct.example.com --server srv" in only
    assert "--vault runner-fixture\n" in only
    assert "--vault host-ex-lab1\n" in only
    assert "for name in host-ex-lab1 runner-fixture; do" in only


def test_a_runner_vm_token_is_stored_in_its_lab_nodes_vault() -> None:
    text = runner_script()
    assert (
        "op item get --account acct.example.com runner-access-token --vault host-ex-lab1"
        in text
    )
    assert "op item create --account acct.example.com --vault host-ex-lab1 -" in text
    assert "host-ex-lab1 already holds runner-access-token; skipping" in text
    assert 'title: "runner-access-token"' in text


def test_the_runner_vm_header_says_what_the_token_reads_and_where_it_goes() -> None:
    text = mixed_script()
    assert "# Each CI runner VM also gets a runner-vm token, which reads only\n" in text
    assert "# the vault runner-fixture, read-only." in text
    assert "item runner-access-token" in text


def test_the_lab_node_of_a_runner_vm_is_its_name_without_the_suffix() -> None:
    assert tokens.node_of("host-a-ci") == "host-a"
    assert tokens.node_of("host-a") == "host-a"


def test_select_runners_takes_the_named_vms_in_name_order() -> None:
    assert tokens.select_runners(CI_VMS, AGENTS, ["ex-lab1-ci", "ex-ctl1-ci"]) == (
        "ex-ctl1-ci",
        "ex-lab1-ci",
    )


def test_select_runners_refuses_a_vm_that_is_not_in_the_ci_group() -> None:
    with pytest.raises(CliError) as caught:
        tokens.select_runners(CI_VMS, AGENTS, ["ex-lab1-ci", "ex-zzz-ci", "ex-aaa-ci"])
    assert str(caught.value) == (
        "not a CI runner VM in the inventory: ex-aaa-ci, ex-zzz-ci"
    )


@pytest.mark.parametrize("vm", ["ex-lab1", "ex-gone-ci"])
def test_select_runners_refuses_a_vm_whose_node_is_no_agent_host(vm: str) -> None:
    with pytest.raises(CliError) as caught:
        tokens.select_runners((*CI_VMS, vm), AGENTS, [vm])
    assert str(caught.value) == (
        f"the lab node of {vm} is not an agent host in the inventory:"
        " its token has no vault to go in"
    )


def test_a_runner_vm_token_goes_from_create_to_its_nodes_vault(tmp_path: Path) -> None:
    done = run_live(
        tmp_path,
        hosts=(),
        runner_vms=(RUNNER_VM,),
        vaults=("host-0-shared", "runner-fixture", "host-ex-lab1"),
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8").splitlines()
    assert [a for a in argv if a.startswith("connect token create")] == [
        (
            "connect token create --account acct.example.com"
            " runner-vm:ex-lab1-ci:20261108T142100Z"
            " --server srv --vault runner-fixture,r"
        )
    ]
    assert argv[-1] == "item create --account acct.example.com --vault host-ex-lab1 -"
    assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == "secret-token"


def test_a_runner_vm_whose_node_vault_holds_the_item_is_skipped(tmp_path: Path) -> None:
    done = run_live(
        tmp_path,
        held=["host-ex-lab1"],
        hosts=(),
        runner_vms=(RUNNER_VM,),
        vaults=("host-0-shared", "runner-fixture", "host-ex-lab1"),
    )
    assert (done.returncode, done.stdout) == (0, "")
    assert done.stderr == "host-ex-lab1 already holds runner-access-token; skipping\n"
    argv = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert "token create" not in argv
    assert "item create" not in argv
