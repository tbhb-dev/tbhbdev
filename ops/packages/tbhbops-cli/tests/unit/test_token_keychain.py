"""Tests for the local and remote keychain commands in Connect token scripts."""

import os
import stat
import subprocess
from pathlib import Path

import pytest
from tbhbops_cli.token_keychain import (
    LINE_MAX,
    NOT_FOUND,
    STORE_LINE,
    TOKEN_MAX,
    _ssh,
    keychain_block,
)

SH = "/bin/sh"
HOST = "ex-ctl1"
WHEN = "20261108T142100Z"
ACCOUNT = "--account acct.example.com"
SSH_FLAGS = "-o IdentityAgent=none -o IdentitiesOnly=yes -i"
FAKE_TOKEN = {
    "ok": "echo secret-token",
    "longest": f"echo {'a' * TOKEN_MAX}",
    "over": f"echo {'a' * (TOKEN_MAX + 1)}",
    "empty": "exit 0",
    "invalid": "echo 'bad token'",
}


@pytest.fixture(params=["ops-controller", "ops-session"])
def prefix(request: pytest.FixtureRequest) -> str:
    return request.param


def write_command(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(stat.S_IRWXU)


def shell_env(root: Path, bin_dir: Path) -> dict[str, str]:
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(root)}


def run_keychain(
    root: Path,
    prefix: str,
    *,
    look: tuple[int, int] = (NOT_FOUND, 0),
    create: str = "ok",
    store: int = 0,
    delete: int = 0,
    prepare: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run one keychain block with fake op, ssh and security commands."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    write_command(
        bin_dir / "op",
        f"""echo "op $*" >> {root}/argv.log
case "$1 $2" in
  "connect token")
    case "$3" in
      delete) exit {delete} ;;
      *) {FAKE_TOKEN[create]} ;;
    esac ;;
esac
""",
    )
    search = f"""echo x >> {root}/looks.log
    if [ "$(wc -l < {root}/looks.log)" -eq 1 ]; then exit {look[0]}; fi
    exit {look[1]}"""
    remote = prefix == "ops-controller"
    if remote:
        write_command(bin_dir / "hostname", "echo ex-controller\n")
        write_command(
            bin_dir / "ssh",
            f"""if [ {prepare} -ne 0 ]; then cat >/dev/null; exit {prepare}; fi
echo "ssh $*" >> {root}/argv.log
case "$*" in
  *"security -i"*) cat > {root}/stdin.log; exit {store} ;;
  *find-generic-password*) {search} ;;
  *"sh -s"*) cat > {root}/prepare.log ;;
esac
""",
        )
    else:
        write_command(
            bin_dir / "security",
            f"""if [ {prepare} -ne 0 ]; then exit {prepare}; fi
echo "security $*" >> {root}/argv.log
case "$1" in
  -i) cat > {root}/stdin.log; exit {store} ;;
  find-generic-password) {search} ;;
esac
""",
        )
    block = keychain_block(
        HOST, "srv", ACCOUNT, when=WHEN, plan=(), say=str, dry_run=False, remote=remote
    )
    return subprocess.run(
        [SH],
        input="\n".join(["set -eu", *block, ""]),
        capture_output=True,
        text=True,
        check=False,
        env=shell_env(root, bin_dir),
    )


def argv(root: Path) -> str:
    return (root / "argv.log").read_text(encoding="utf-8")


def test_the_token_goes_to_security_on_stdin_and_never_into_argv(
    tmp_path: Path, prefix: str
) -> None:
    done = run_keychain(tmp_path, prefix)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    logged = argv(tmp_path)
    assert "secret-token" not in logged
    assert "item get" not in logged
    assert "item create" not in logged
    assert "token delete" not in logged
    assert logged.count("find-generic-password") == 2
    lines = logged.splitlines()
    if prefix == "ops-controller":
        assert lines[-2] == (
            f"ssh {SSH_FLAGS} {tmp_path}/.ssh/sessions/ex-controller_ed25519 ops@{HOST}"
            ' security unlock-keychain -p "" ops-connect.keychain && security -i'
        )
        assert "create-keychain" in (tmp_path / "prepare.log").read_text("utf-8")
    else:
        assert "ssh" not in logged
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


def test_every_ssh_runs_as_ops_with_the_controller_session_key_and_no_sudo(
    tmp_path: Path,
) -> None:
    done = run_keychain(tmp_path, "ops-controller")
    assert done.returncode == 0
    logged = argv(tmp_path)
    assert "sudo" not in logged
    ssh = [line for line in logged.splitlines() if line.startswith("ssh ")]
    assert len(ssh) == 4
    for line in ssh:
        assert (
            line.removeprefix("ssh -n ")
            .removeprefix("ssh ")
            .startswith(
                f"{SSH_FLAGS} {tmp_path}/.ssh/sessions/ex-controller_ed25519"
                f" ops@{HOST} "
            )
        ), line


@pytest.mark.parametrize("controller", ["mac-a", "mac-b"])
def test_the_operator_shell_expands_the_session_key_at_run_time(
    tmp_path: Path, controller: str
) -> None:
    # A space in HOME must remain inside the single identity argument.
    root = tmp_path / "operator home"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    write_command(
        bin_dir / "hostname",
        f'[ "$1" = "-s" ] || exit 1\necho {controller}\n',
    )
    write_command(bin_dir / "ssh", 'printf "%s\\n" "$@"\n')
    done = subprocess.run(
        [SH],
        input=_ssh(HOST, "true"),
        capture_output=True,
        text=True,
        check=True,
        env=shell_env(root, bin_dir),
    )
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "-o",
        "IdentityAgent=none",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(root / ".ssh" / "sessions" / f"{controller}_ed25519"),
        f"ops@{HOST}",
        "true",
    ]


def test_a_keychain_that_already_holds_the_item_is_skipped_before_any_token(
    tmp_path: Path, prefix: str
) -> None:
    done = run_keychain(tmp_path, prefix, look=(0, 0))
    assert (done.returncode, done.stdout) == (0, "")
    holder = "ops" if prefix == "ops-controller" else "your keychain"
    assert done.stderr == f"{holder} on {HOST} already holds ops-connect; skipping\n"
    logged = argv(tmp_path)
    assert "token create" not in logged
    assert "security -i" not in logged
    assert not (tmp_path / "stdin.log").exists()


def test_a_failed_keychain_search_stops_before_token_creation(
    tmp_path: Path, prefix: str
) -> None:
    done = run_keychain(tmp_path, prefix, look=(255, 0))
    assert done.returncode == 1
    keychain = "the ops keychain" if prefix == "ops-controller" else "your keychain"
    assert done.stderr == f"could not look in {keychain} on {HOST}\n"
    assert "token create" not in argv(tmp_path)


@pytest.mark.parametrize("create", ["empty", "invalid"])
def test_an_invalid_token_is_never_sent_to_security(
    tmp_path: Path, prefix: str, create: str
) -> None:
    done = run_keychain(tmp_path, prefix, create=create)
    assert done.returncode == 1
    if create == "empty":
        assert (
            done.stderr
            == f"op connect token create printed no token for {prefix} {HOST}\n"
        )
    else:
        assert done.stderr == (
            f"the {prefix} token for {HOST} has a character the keychain line"
            " cannot carry\n"
        )
    assert not (tmp_path / "stdin.log").exists()


def test_the_longest_token_the_keychain_line_can_carry_is_stored(
    tmp_path: Path, prefix: str
) -> None:
    done = run_keychain(tmp_path, prefix, create="longest")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    stored = (tmp_path / "stdin.log").read_text(encoding="utf-8")
    assert len(stored) - 1 == LINE_MAX
    assert "token delete" not in argv(tmp_path)


def test_the_token_max_leaves_room_for_the_rest_of_the_keychain_line() -> None:
    rest = STORE_LINE.replace("$tok", "")
    assert LINE_MAX == 4095
    assert TOKEN_MAX + len(rest) == LINE_MAX


@pytest.mark.parametrize(("create", "store"), [("over", 0), ("ok", 1)])
def test_a_token_that_cannot_be_stored_is_deleted(
    tmp_path: Path, prefix: str, create: str, store: int
) -> None:
    done = run_keychain(tmp_path, prefix, create=create, store=store)
    assert (done.returncode, done.stdout) == (1, "")
    name = f"{prefix}:{HOST}:{WHEN}"
    deleted = f"deleted the {prefix} token {name} from srv\n"
    logged = argv(tmp_path)
    if create == "over":
        assert done.stderr == (
            f"the {prefix} token for {HOST} has {TOKEN_MAX + 1} characters,"
            f" over the {TOKEN_MAX} the keychain line can carry\n" + deleted
        )
        assert "security -i" not in logged
        assert "a" * 20 not in logged
        assert not (tmp_path / "stdin.log").exists()
    else:
        assert (
            done.stderr
            == f"the {prefix} token for {HOST} was made but not stored\n" + deleted
        )
        assert "secret-token" not in logged
        assert (tmp_path / "stdin.log").exists()
    assert logged.count("op connect token delete") == 1
    assert (
        logged.splitlines()[-1]
        == f"op connect token delete {ACCOUNT} {name} --server srv"
    )


@pytest.mark.parametrize(("create", "store"), [("over", 0), ("ok", 1)])
def test_a_token_that_cannot_be_deleted_is_left_for_deleting_by_hand(
    tmp_path: Path, prefix: str, create: str, store: int
) -> None:
    done = run_keychain(tmp_path, prefix, create=create, store=store, delete=1)
    assert (done.returncode, done.stdout) == (1, "")
    name = f"{prefix}:{HOST}:{WHEN}"
    assert done.stderr.endswith(
        f"could not delete the {prefix} token {name};"
        " delete it by hand with op connect token delete\n"
    )
    assert "deleted the" not in done.stderr
    assert argv(tmp_path).splitlines()[-1] == (
        f"op connect token delete {ACCOUNT} {name} --server srv"
    )


def test_a_keychain_that_lacks_the_item_after_the_store_fails_the_script(
    tmp_path: Path, prefix: str
) -> None:
    done = run_keychain(tmp_path, prefix, look=(NOT_FOUND, NOT_FOUND))
    assert done.returncode == 1
    keychain = "the ops keychain" if prefix == "ops-controller" else "your keychain"
    assert done.stderr == f"{keychain} on {HOST} does not hold the token\n"


def test_a_failed_keychain_preparation_stops_the_script_first(
    tmp_path: Path, prefix: str
) -> None:
    done = run_keychain(tmp_path, prefix, prepare=1)
    assert done.returncode == 1
    keychain = "the ops keychain" if prefix == "ops-controller" else "your keychain"
    assert done.stderr == f"could not prepare {keychain} on {HOST}\n"
    assert not (tmp_path / "argv.log").exists()
