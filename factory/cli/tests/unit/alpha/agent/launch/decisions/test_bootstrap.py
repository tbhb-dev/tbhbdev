"""Check the shell scripts for the shipped CLI."""

import ast
import shlex
from dataclasses import replace

import pytest
from agent_example import LINUX

from tbhbdev.alpha.agent.launch.decisions import bootstrap
from tbhbdev.alpha.agent.model import Invalid, Profile


def test_receive_claims_before_extracting() -> None:
    script = bootstrap.receive("run", "sockets", "0" * 64, "3.15")
    session_check = 'tmux_state has-session ag-run\ntest "$present" -eq 0 || exit 73'
    owner_input = "IFS= read -r given || exit 73"
    claim = r"""( printf '%s\n' "$given" > "$d/run.owner" ) || exit 73"""
    assert script.index(session_check) < script.index(owner_input)
    assert script.index(owner_input) < script.index("set -C") < script.index(claim)
    assert script.index(claim) < script.index('tar -xf - -C "$d"')
    assert 'for p in "$d/run."*' in script


def test_receive_checks_digest_before_unpacking_and_installing() -> None:
    digest = "1234" * 16
    python = "/some path/python with 'quotes'"
    script = bootstrap.receive("run", "sockets", digest, python)
    steps = [
        'tar -xf - -C "$d"',
        'archive="$d/run.cli.tar"',
        f'test "${{digest%% *}}" = {digest} || exit 65',
        'cli="$d/run.cli"',
        'tar -xf "$archive" -C "$cli/lib"',
        'rm -f "$archive"',
        f'uv venv --python {shlex.quote(python)} "$cli/venv"',
        'uv pip install --python "$cli/venv/bin/python" -r "$cli/lib/requirements.txt"',
    ]
    positions = [script.index(step) for step in steps]
    assert positions == sorted(positions)
    assert 'sha256sum < "$archive"' in script
    assert 'shasum -a 256 < "$archive"' in script
    assert "trap 'rm -rf \"$cli\"' EXIT" in script
    assert script.endswith("trap - EXIT\n")


def test_receive_writes_isolated_entry_points() -> None:
    script = bootstrap.receive("run", "sockets", "0" * 64, "3.15")
    marker = '"$cli/venv/bin/python" -I - "$cli" <<\'PY\'\n'
    entry_points = script.split(marker)[1].split("\nPY\n")[0]
    ast.parse(entry_points)
    assert 'sysconfig.get_path("purelib")' in entry_points
    assert '(site / "tbhbdev-bundle.pth").write_text' in entry_points
    assert "cli / 'lib'" in entry_points
    assert 'shlex.quote(str(cli / "venv/bin/python"))' in entry_points
    assert '(("tbhbdev", ""), ("gh", " alpha gh-shim"))' in entry_points
    assert r'#!/bin/sh\nexec {python} -I -m tbhbdev{args} "$@"\n' in entry_points
    assert "entry.chmod(0o700)" in entry_points


@pytest.mark.parametrize(("steer", "extra"), [(False, ""), (True, " --steer")])
def test_start_dispatches_shipped_program(steer: bool, extra: str) -> None:
    digest = "1234" * 16
    shell, option, script = bootstrap.start(
        "run", "sockets", LINUX.name, digest, steer=steer
    )
    assert (shell, option) == ("/bin/sh", "-c")
    assert 'owner="$d/run.owner"' in script
    assert 'test "$(cat "$owner")" = "$given" || exit 73' in script
    expected = shlex.split(
        "tmux -S '$s/agents' -f /dev/null new-session -d -s ag-run "
        "'$d/run.cli/bin/tbhbdev' alpha agent run run --host "
        f"{shlex.quote(LINUX.name)} --owned --cli-sha256 {digest}{extra}"
    )
    assert shlex.split(script.splitlines()[-1]) == expected


def test_check_slot_uses_shipped_program() -> None:
    config = replace(LINUX, tbhbdev="/unused", max_concurrent_runs=3)
    script = bootstrap.check_slot("run", config, Profile.BUILD)
    assert script.startswith("# tbhbdev-check-slot\n")
    assert shlex.split(script.splitlines()[-1]) == [
        "exec",
        "$d/run.cli/bin/tbhbdev",
        "alpha",
        "agent",
        "check-slot",
        config.name,
        "build",
        "3",
        "--",
        *config.accounts,
    ]


def test_remove_cli_targets_bundle_directory() -> None:
    script = bootstrap.remove_cli("run")
    assert 'd="$state/tbhbdev/agent-runs"' in script
    assert script.splitlines()[-1] == 'rm -rf "$d/run.cli"'


def test_remove_cli_rejects_invalid_run_name() -> None:
    with pytest.raises(Invalid, match="run name"):
        bootstrap.remove_cli("../run")
