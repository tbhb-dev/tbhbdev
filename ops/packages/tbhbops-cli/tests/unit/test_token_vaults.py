"""Tests for provisioning vaults and storing Connect token items."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
lazy from collections.abc import Collection

import pytest
from tbhbops_cli import token_policy, tokens
from tbhbops_cli.token_vaults import VAULT_JQ

AGENTS = ("ex-ctl1", "ex-ctl2", "ex-lab1", "ex-win1")
CONTROLLERS = ("ex-ctl1", "ex-ctl2")
WHEN = "20261108T142100Z"
ACCOUNT = "acct.example.com"
SH = "/bin/sh"
RUNNER_VM = "ex-lab1-ci"
NEEDED = (
    "host-0-shared",
    *(f"host-{h}" for h in AGENTS),
    "host-storage-nas-a",
    "host-storage-nas-b",
)

SKIPPED = "".join(
    f"ops on {h} already holds ops-connect; skipping\n" for h in CONTROLLERS
)

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
        input=tokens.commands(
            "srv",
            hosts,
            AGENTS,
            CONTROLLERS,
            WHEN,
            account=ACCOUNT,
            ops_hosts=token_policy.select_ops(CONTROLLERS, hosts),
            runner_vms=runner_vms,
            dry_run=False,
        ),
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


def test_a_rerun_after_everything_is_made_skips_every_host_and_exits_zero(
    tmp_path: Path,
) -> None:
    done = run_live(
        tmp_path, held=("host-ex-ctl1", "host-ex-lab1"), hosts=("ex-ctl1", "ex-lab1")
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
        ["jq", "-r", "--arg", "name", name, VAULT_JQ],
        input=vault_list(vaults),
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == answer + "\n"


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
