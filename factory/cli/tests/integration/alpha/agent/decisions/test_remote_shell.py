"""Check the shared shell and ownership guards on a scratch run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from shutil import which
from tempfile import mkdtemp

import pytest
from agent_driver_example import write_standalone_fake
from agent_example import LINUX, settings
from remote_script_example import cleanup, execute, no_session

from tbhbdev.alpha.agent.decisions import (
    policy,
    remote_cleanup,
    remote_host,
    remote_inspection,
    remote_run,
    remote_shell,
)


@pytest.mark.parametrize("bad", ["file", "symlink", "mode", "socket-link"])
def test_socket_directory_refuses_unsafe_paths(host: Path, bad: str) -> None:
    directory = host / "sockets"
    if bad == "file":
        directory.write_text("file")
    elif bad == "symlink":
        directory.symlink_to(host, target_is_directory=True)
    else:
        directory.mkdir()
        directory.chmod(0o755 if bad == "mode" else 0o700)
        if bad == "socket-link":
            (directory / "agents").symlink_to(host / "elsewhere")
    assert execute(remote_run.read("sample", settings().sockets_dir)).returncode == 65


@pytest.mark.parametrize("suffix", [".keep", ".rc.pending"])
def test_exclusive_markers_refuse_symlinks(host: Path, suffix: str) -> None:
    target = host / "other-file"
    target.write_text("preserve")
    path = host / "state/tbhbdev/agent-runs" / ("sample" + suffix)
    path.symlink_to(target)
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    script = (
        remote_cleanup.keep("sample")
        if suffix == ".keep"
        else remote_run.fallback("sample", settings().sockets_dir)
    )
    assert execute(script).returncode != 0
    assert target.read_text() == "preserve"
    assert path.is_symlink()


def test_scripts_have_fixed_path_socket_and_git_environment() -> None:
    config = settings()
    scripts = [
        remote_run.session("sample", "kill-session", config.sockets_dir),
        remote_run.transfer("sample", config.sockets_dir),
        remote_run.start("sample", config.sockets_dir, LINUX)[2],
        remote_run.read("sample", config.sockets_dir),
        remote_run.read("sample", config.sockets_dir, answer=True),
        remote_run.fallback("sample", config.sockets_dir),
        remote_cleanup.clean(
            "example-org/example-repo",
            "sample",
            "stamp",
            config.sockets_dir,
            force=False,
        ),
    ]
    for script in scripts:
        assert script.startswith("PATH=/usr/bin:/bin:/usr/sbin:/sbin\nexport PATH\n")
        assert 'PATH="$HOME/.local/bin:$PATH"' in script
        assert 'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"' in script
        assert 'tmux -S "$s/agents" -f /dev/null' in script
        assert "tmux -L" not in script
        assert f"s={config.sockets_dir}\n" in script
    assert " git " not in scripts[-1]
    assert "agent-mirror.lock" not in scripts[-1]
    assert remote_host.installed(LINUX).endswith('"$HOME/.local/bin/tbhbdev" --version')
    assert "-lc" not in remote_run.start("sample", config.sockets_dir, LINUX)[2]
    for script in [
        remote_host.preflight("example-org/example-repo", "main", "example", LINUX),
    ]:
        for line in script.splitlines():
            if " git " in line:
                assert 'env -i PATH="$PATH" HOME="$HOME" LANG=C LC_ALL=C' in line
                assert (
                    "GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1 GIT_CONFIG_NOSYSTEM=1"
                    in line
                )
                assert "GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false" in line
                assert "-c core.fsmonitor=false -c core.sshCommand=ssh" in line
                assert "-c credential.helper= -c core.askPass=" in line
                assert "-c protocol.ext.allow=never -c core.pager=cat" in line
                assert ('SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}"' in line) == (
                    "ls-remote" in line
                )
        assert "worktree remove" not in script


@pytest.mark.parametrize("path", [".private/sockets", "~/.private/sockets"])
def test_socket_home_relative_paths(host: Path, path: str) -> None:
    script = remote_shell.socket_root(path) + 'printf "%s" "$s/agents"\n'
    result = execute(script)
    assert result.returncode == 0
    assert result.stdout.decode() == policy.resolve(str(host), path) + "/agents"


@pytest.mark.parametrize("component", ["state", "tbhbdev", "agent-runs"])
def test_run_directory_symlinks_are_not_followed(host: Path, component: str) -> None:
    root = host / "state/tbhbdev/agent-runs"
    (root / "sample.log").write_text("private marker")
    path = next(p for p in (root, *root.parents) if p.name == component)
    path.rename(host / "private")
    path.symlink_to(host / "private", target_is_directory=True)
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    result = execute(remote_run.read("sample", settings().sockets_dir))
    assert result.returncode == 65
    assert b"private marker" not in result.stdout


@pytest.mark.parametrize(
    "script",
    [
        "read",
        "fallback",
        "session",
        "keep",
        "clean",
        "log",
        "diff",
        "start",
    ],
)
@pytest.mark.parametrize("owner_kind", ["other", "missing", "symlink"])
def test_owner_check_precedes_every_effect(
    host: Path, script: str, owner_kind: str
) -> None:
    if script == "clean" and owner_kind == "missing":
        no_session(host)
    owner = host / "state/tbhbdev/agent-runs/sample.owner"
    owner.rename(host / "saved-owner")
    if owner_kind == "other":
        owner.write_text("another-owner\n")
    elif owner_kind == "symlink":
        owner.symlink_to(host / "saved-owner")
    scripts = {
        "read": remote_run.read("sample", settings().sockets_dir),
        "fallback": remote_run.fallback("sample", settings().sockets_dir),
        "session": remote_run.session("sample", "kill-session", settings().sockets_dir),
        "keep": remote_cleanup.keep("sample"),
        "clean": cleanup(),
        "log": remote_inspection.inspect(
            "example-org/example-repo", "sample", limit=10
        ),
        "diff": remote_inspection.inspect(
            "example-org/example-repo", "sample", limit=None
        ),
        "start": remote_run.start("sample", settings().sockets_dir, LINUX)[2],
    }
    result = execute(scripts[script])
    if script == "clean" and owner_kind == "missing":
        assert result.returncode == 0
        assert execute(scripts[script]).returncode == 0
        return
    assert result.returncode == 73
    assert not (host / "sockets").exists()
    assert not (host / "state/tbhbdev/agent-runs/trash").exists()


@pytest.mark.parametrize("verb", ["kill", "clean"])
def test_real_tmux_preserves_longer_session(host: Path, verb: str) -> None:
    tmux = which("tmux", path="/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin")
    if tmux is None:
        pytest.skip("tmux is missing")
    (host / "bin/tmux").symlink_to(tmux)
    # Keep the socket below Unix's path limit even with long pytest roots.
    sockets = Path(mkdtemp(prefix="tmux-"))
    command = [tmux, "-S", str(sockets / "agents"), "-f", "/dev/null"]
    state = host / "state/tbhbdev/agent-runs"
    (state / "X.owner").write_text("example-owner\n")
    try:
        for name in ("ag-X", "ag-X2"):
            subprocess.run(
                [*command, "new-session", "-d", "-s", name, "sleep 120"],
                check=True,
            )
        subprocess.run([*command, "kill-session", "-t", "=ag-X"], check=True)
        script = (
            remote_run.session("X", "kill-session", str(sockets))
            if verb == "kill"
            else remote_cleanup.clean(
                "example-org/example-repo",
                "X",
                "stamp",
                str(sockets),
                force=True,
            )
        )
        result = execute(script)
        assert result.returncode == 0, result.stderr
        subprocess.run([*command, "has-session", "-t", "=ag-X2"], check=True)
    finally:
        subprocess.run([*command, "kill-server"], check=False, capture_output=True)
        sockets.rename(host / "sockets")


@pytest.mark.parametrize("xdg", [None, "", "relative/state", "absolute"])
def test_remote_state_matches_run_host(
    host: Path, monkeypatch: pytest.MonkeyPatch, xdg: str | None
) -> None:
    if xdg is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv(
            "XDG_STATE_HOME", str(host / "state") if xdg == "absolute" else xdg
        )
    result = execute(remote_shell.ROOT + "printf '%s' \"$d\"")
    expected = host / ("state" if xdg == "absolute" else ".local/state")
    assert result.returncode == 0
    assert result.stdout.decode() == str(expected / "tbhbdev/agent-runs")


def test_locale_precedes_tmux(host: Path) -> None:
    write_standalone_fake(
        host / "bin/tmux",
        "import os, sys\n"
        "assert os.environ['LANG'] == os.environ['LC_ALL'] == 'C'\n"
        "sys.stderr.write('no server running on example')\nsys.exit(1)\n",
    )
    assert execute(remote_run.read("sample", settings().sockets_dir)).returncode == 0
