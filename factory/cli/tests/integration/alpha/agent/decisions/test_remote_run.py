"""Exercise remote run claims, sessions, status, and logs."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from agent_driver_example import write_standalone_fake
from agent_example import LINUX, settings
from remote_script_example import execute, execute_ssh, no_session, with_test_paths

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import (
    remote_host,
    remote_run,
)


@pytest.mark.parametrize("error", ["unrelated failure", "missing"])
@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_tmux_error_never_writes_rc(host: Path, error: str, os_name: str) -> None:
    write_standalone_fake(host / "bin/uname", f"print({os_name!r})\n")
    if error != "missing":
        write_standalone_fake(
            host / "bin/tmux",
            "import sys\nsys.stderr.write('unrelated failure')\nsys.exit(1)\n",
        )
    else:
        # A fixed PATH containing only scratch tools also tests a missing tmux.
        for name in ("mkdir", "chmod", "mv", "find", "id", "cat"):
            program = next(
                p
                for p in (
                    Path("/usr/bin") / name,
                    Path("/bin") / name,
                )
                if p.exists()
            )
            (host / "bin" / name).symlink_to(program)
        os.environ["PATH"] = str(host / "bin")
    result = execute(remote_run.fallback("sample", settings().sockets_dir))
    assert result.returncode == 75
    assert not (host / "state/tbhbdev/agent-runs/sample.rc").exists()


def test_session_is_checked_before_rc(host: Path) -> None:
    write_standalone_fake(
        host / "bin/tmux",
        "import os, sys\nfrom pathlib import Path\n"
        "root = Path(os.environ['XDG_STATE_HOME'])\n"
        "(root / 'tbhbdev/agent-runs/sample.rc').write_text('0')\n"
        'sys.stderr.write("can\'t find session: ag-sample\\n")\nsys.exit(1)\n',
    )
    result = execute(remote_run.read("sample", settings().sockets_dir))
    assert result.returncode == 0
    assert result.stdout.splitlines()[1] == b"30"


@pytest.mark.parametrize(
    "case",
    [
        (".phase", b"running\n0\n1\n1", False, None),
        (".rc", b"256", False, None),
        (".rc", b"x", False, None),
        (".out", b"answer\xff", True, "answer\ufffd"),
        (".log", b"\x1b[31m hello\x07", False, "[31m hello"),
        (".log", None, False, None),
    ],
)
def test_untrusted_run_files(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, bytes | None, bool, str | None],
) -> None:
    suffix, content, answer, expected = case
    write_standalone_fake(
        host / "bin/tmux",
        "import sys\n"
        'sys.stderr.write("can\'t find session: ag-sample\\n")\nsys.exit(1)\n',
    )
    path = host / "state/tbhbdev/agent-runs" / ("sample" + suffix)
    if content is None:
        target = host / "other-file"
        target.write_text("must never read this")
        path.symlink_to(target)
    else:
        path.write_bytes(content)

    monkeypatch.setattr(transport, "execute", execute_ssh)
    if expected is None:
        with pytest.raises((ValueError, transport.TransportError)):
            transport.read(
                settings(),
                LINUX,
                ("sample", "example-owner"),
                answer=answer,
            )
    else:
        assert (
            transport.read(
                settings(),
                LINUX,
                ("sample", "example-owner"),
                answer=answer,
            ).text
            == expected
        )


def test_start_command_and_absolute_program() -> None:
    command = remote_run.start("example", "/example/sockets", LINUX)
    assert command[:2] == ("/bin/sh", "-c")
    assert shlex.split(command[2].splitlines()[-1]) == shlex.split(
        "tmux -S '$s/agents' -f /dev/null new-session -d -s ag-example "
        "'$HOME/.local/bin/tbhbdev' alpha agent run-host example --host example-linux "
        "--owned"
    )
    host = replace(LINUX, tbhbdev="/example path/bin/tbhbdev")
    assert remote_host.program(host) == "'/example path/bin/tbhbdev'"
    assert remote_host.installed(host).endswith("'/example path/bin/tbhbdev' --version")


def test_read_defaults_to_bounded_log(host: Path) -> None:
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.log").write_bytes(b"L" * 70000)
    (state / "sample.out").write_bytes(b"O" * 70000)
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    result = execute(remote_run.read("sample", settings().sockets_dir))
    assert result.returncode == 0
    assert result.stdout.split(b"\n", 4)[4] == b"L" * 65536
    result = execute(remote_run.read("sample", settings().sockets_dir, answer=True))
    assert result.returncode == 0
    assert result.stdout.split(b"\n", 4)[4] == b"O" * 65536


def test_claim_refusal_is_distinct_even_for_redirection_failure(host: Path) -> None:
    # A normal directory collision is checked before the exclusive creation.
    # Isolate the creation step too: sh treats ':' as a special builtin.
    claim = host / "existing"
    claim.write_text("preserve")
    script = remote_run.transfer("sample", settings().sockets_dir)
    line = next(line for line in script.splitlines() if '> "$d/sample.owner"' in line)
    line = line.replace('"$d/sample.owner"', shlex.quote(str(claim)))
    result = execute("set -C\n" + line)
    assert result.returncode == 73
    assert claim.read_text() == "preserve"


def test_session_target_never_matches_another_runs_prefix(host: Path) -> None:
    marker = host / "other-session-stopped"
    write_standalone_fake(
        host / "bin/tmux",
        "import sys\nfrom pathlib import Path\n"
        "if sys.argv[-1] == '=ag-sample':\n"
        '    sys.stderr.write("can\'t find session: ag-sample")\n'
        "    sys.exit(1)\n"
        f"Path({str(marker)!r}).write_text('stopped ag-sample-other')\n",
    )
    result = execute(
        remote_run.session("sample", "kill-session", settings().sockets_dir)
    )
    assert result.returncode == 0
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_claim_rejects_existing_owner(host: Path, kind: str) -> None:
    no_session(host)
    owner = host / "state/tbhbdev/agent-runs/sample.owner"
    owner.rename(host / "preserved-owner")
    if kind == "file":
        owner.write_text("other-owner\n")
    else:
        owner.symlink_to(host / ("preserved-owner" if kind == "symlink" else "missing"))
    result = execute(remote_run.transfer("sample", settings().sockets_dir))
    assert result.returncode == 73
    assert (host / "preserved-owner").read_text() == "example-owner\n"
    assert (
        owner.is_symlink() if kind != "file" else owner.read_text() == "other-owner\n"
    )


def test_concurrent_claims_have_one_winner(host: Path) -> None:
    no_session(host)
    (host / "state/tbhbdev/agent-runs/sample.owner").rename(host / "preserved-owner")
    # Replace tar so the transfer reaches a successful end without input files.
    write_standalone_fake(host / "bin/tar", "")
    (host / "sockets").mkdir(mode=0o700)
    script = with_test_paths(remote_run.transfer("sample", settings().sockets_dir))
    processes = [
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for process, owner in zip(processes, ("first", "second"), strict=True):
        process.communicate(f"{owner}\n".encode(), timeout=10)
    assert sorted(p.returncode for p in processes) == [0, 73]
    assert (host / "state/tbhbdev/agent-runs/sample.owner").read_text() in {
        "first\n",
        "second\n",
    }


def test_real_ssh_shell_quoting(host: Path) -> None:
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.log").write_text("literal ' $() ; bytes\n")
    script = with_test_paths(remote_run.read("sample", settings().sockets_dir))
    # The first shell parses the ssh command string; the second runs the script.
    result = subprocess.run(
        ["/bin/sh", "-c", shlex.join(("/bin/sh", "-c", script))],
        input=b"example-owner\n",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.endswith(b"literal ' $() ; bytes\n")


def test_start_with_matching_owner(host: Path) -> None:
    marker = host / "started"
    write_standalone_fake(
        host / "bin/tmux",
        "from pathlib import Path\nimport sys\n"
        "assert sys.argv[3:] == ['-f', '/dev/null', 'new-session', '-d', '-s',\n"
        "    'ag-sample', '/example path/bin/tbhbdev', 'alpha', 'agent', 'run-host',\n"
        "    'sample', '--host', 'example-linux', '--owned']\n"
        f"Path({str(marker)!r}).write_text('started')\n",
    )
    config = replace(LINUX, tbhbdev="/example path/bin/tbhbdev")
    assert (
        execute(
            remote_run.start("sample", settings().sockets_dir, config)[2]
        ).returncode
        == 0
    )
    assert marker.read_text() == "started"


def test_launch_finds_tmux_in_the_agent_accounts_local_bin(host: Path) -> None:
    directory = host / ".local/bin"
    directory.mkdir(parents=True)
    write_standalone_fake(directory / "tmux", 'print("account-tmux")\n')
    result = execute(remote_run.start("sample", settings().sockets_dir, LINUX)[2])
    assert result.returncode == 0
    assert result.stdout == b"account-tmux\n"
