"""Tests for the complete Connect token scripts."""

import re
import subprocess
from pathlib import Path

import pytest
from tbhbops_cli import token_policy, token_script

AGENTS = ("ex-ctl1", "ex-ctl2", "ex-lab1", "ex-win1")
CONTROLLERS = ("ex-ctl1", "ex-ctl2")
WHEN = "20261108T142100Z"
ACCOUNT = "acct.example.com"
HERE = Path(__file__).parents[1]
SH = "/bin/sh"
OPS_VAULTS = ("host-storage-nas-a", "host-storage-nas-b")
NEEDED = ("host-0-shared", *(f"host-{h}" for h in AGENTS), *OPS_VAULTS)


def script(
    hosts: tuple[str, ...] = AGENTS,
    *,
    session: str | None = None,
    runner_vms: tuple[str, ...] = (),
    dry_run: bool = False,
) -> str:
    ops = token_policy.select_ops(CONTROLLERS, hosts)
    return token_script.commands(
        "srv",
        hosts,
        AGENTS,
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


def test_a_server_name_with_a_space_is_quoted() -> None:
    text = token_script.commands(
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
    text = token_script.commands(
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
    text = token_script.commands(
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
    dry = token_script.commands(
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


SESSION_NAME = "ops-session:ex-ctl1:20261108T142100Z"


def test_only_the_ops_session_token_reaches_the_tailnet_oauth_vault() -> None:
    assert token_policy.VAULT_OAUTH == "overlay-oauth"
    for host in AGENTS:
        assert token_policy.VAULT_OAUTH not in {
            g.vault for g in token_policy.grants(host, AGENTS, CONTROLLERS)
        }
    assert token_policy.VAULT_OAUTH not in {g.vault for g in token_policy.ops_grants()}
    plain = script()
    assert "overlay-oauth" not in plain
    text = script(("ex-lab1",), session="ex-ctl1")
    assert text.count("overlay-oauth") >= 4
    assert "op vault create" in text
    assert "--vault overlay-oauth,r)" in text
    assert "--server srv --vault overlay-oauth\n" in text
    assert "for name in host-0-shared host-ex-ctl1 host-ex-ctl2 host-ex-lab1" in text
    assert " overlay-oauth; do" in text


def test_the_ops_session_token_is_named_and_scoped_like_an_operator_macs() -> None:
    text = script(("ex-lab1",), session="ex-ctl1")
    assert (
        f"tok=$(op connect token create --account {ACCOUNT} '{SESSION_NAME}'"
        " --server srv --vault host-0-shared --vault host-ex-ctl1,r"
        " --vault host-ex-ctl2,r --vault host-ex-lab1,r --vault host-ex-win1,r"
        " --vault overlay-oauth,r)"
    ) in text
    assert token_policy.SESSION_PREFIX == "ops-session"
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


def runner_script(*, dry_run: bool = False) -> str:
    """The script for a runner VM alone, which makes only its token."""
    return script((), runner_vms=(RUNNER_VM,), dry_run=dry_run)


def mixed_script() -> str:
    """The script for an agent host and a runner VM, which makes both kinds."""
    return script(("ex-lab1",), runner_vms=(RUNNER_VM,))


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
    assert token_policy.runner_grants() == (token_policy.Grant("runner-fixture", "r"),)
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
