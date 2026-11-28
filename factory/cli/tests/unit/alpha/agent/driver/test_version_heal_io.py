"""Launch and synchronize versions with injected Git, SSH, uv and exec calls."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from pwd import getpwuid
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent
from agent_driver_example import execution_arguments
from agent_example import settings
from agent_host_example import commit, git

from tbhbdev import version
from tbhbdev.alpha import version_check
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote_shell
from tbhbdev.alpha.agent.driver import metadata, startup, version_heal
from tbhbdev.alpha.agent.driver.inputs import checked_prompt
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.install import cli_build

CONFIG = settings()
HOST = CONFIG.hosts[0]
HEAD = "a" * 40
OLD = "b" * 40
NEW_LINE = f"tbhbdev 0 ({HEAD})"
OLD_LINE = f"tbhbdev 0 ({OLD})"
URL = "https://github.com/tbhb-dev/tbhbdev"
REF = "refs/heads/main"
GIT_OVERRIDES = ("-c", "credential.helper=", "-c", "http.extraHeader=")
GIT = [
    shutil.which("git", path=version_check.TOOL_PATH) or "git",
    *GIT_OVERRIDES,
    "ls-remote",
    URL,
    REF,
]
INSTALL = ["/bin/sh", "-c", cli_build.script(HEAD, URL)]
SUMMARY = f"{OLD} {HEAD} ok 0.1s"
LIVE = Path(getpwuid(os.getuid()).pw_dir) / ".local/bin/tbhbdev"
RETRY = "_TBHB_AGENT_VERSION_HEAL"
ORIGIN = subprocess.CompletedProcess(GIT, 0, f"{HEAD}\t{REF}\n")
NETWORK_ENV = {
    "HTTPS_PROXY": "http://proxy.example:3128",
    "SSL_CERT_FILE": "/example/uv-ca.pem",
    "GIT_SSL_CAINFO": "/example/git-ca.pem",
}


@pytest.fixture(autouse=True)
def fresh_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RETRY, raising=False)
    for key in NETWORK_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("scope", ["--local", "--global"])
def test_read_origin_ignores_git_url_rewrites(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"}
    git(isolated_home, "init", "--template=", "--initial-branch=main", env=env)
    fake_head = commit(isolated_home, "fake main", env=env)
    key = f"url.{isolated_home.as_uri()}.insteadOf"
    git(isolated_home, "config", scope, key, URL, env=env)
    # Disable real HTTPS while still allowing a rewrite to the test repository.
    command = ["-c", "protocol.https.allow=never", "ls-remote", URL, REF]
    assert git(isolated_home, *command, env=env) == f"{fake_head}\t{REF}"
    monkeypatch.chdir(isolated_home)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(isolated_home / ".gitconfig"))
    status, output = version_heal.read_origin([GIT[0], *command], version_check.TIMEOUT)
    assert status != 0
    assert output == ""


def test_read_origin_uses_fixed_url_and_clean_git_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/example/.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "example")
    run = Mock(return_value=ORIGIN)
    monkeypatch.setattr(subprocess, "run", run)

    assert version_heal.sync(CONFIG, HOST, NEW_LINE, NEW_LINE) == HEAD
    run.assert_called_once()
    assert run.call_args.args == (GIT,)
    cwd = Path(run.call_args.kwargs["cwd"])
    assert cwd.is_absolute()
    assert cwd != Path.cwd()
    home = getpwuid(os.getuid()).pw_dir
    assert run.call_args.kwargs["env"] == {
        "HOME": home,
        "PATH": f"{version_check.TOOL_PATH}:{home}/.local/bin",
        "GIT_CEILING_DIRECTORIES": str(cwd.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
    }


def test_heal_ignores_path_overrides_and_root_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, origin = tmp_path / "root", tmp_path / "origin"
    for repo in (root, origin):
        repo.mkdir()
        git(repo, "init", "--template=", "--initial-branch=main")
    head = commit(origin, "expected main")
    other = commit(root, "rewritten main")
    url = origin.as_uri()
    git(root, "config", "--local", f"url.{root.as_uri()}.insteadOf", url)
    assert git(root, "ls-remote", url, REF) == f"{other}\t{REF}"

    approved = tmp_path / "approved"
    approved.mkdir()
    # Probe uv without reinstalling the CLI. The Bazel test can run without uv.
    home = getpwuid(os.getuid()).pw_dir
    uv = shutil.which("uv", path=f"{version_check.TOOL_PATH}:{home}/.local/bin")
    probe = approved / "uv"
    probe.write_text(f"#!/bin/sh\nexec {shlex.quote(uv or '/bin/true')} --version\n")
    probe.chmod(0o755)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    for name in ("git", "uv"):
        binary = shadow / name
        binary.write_text("#!/bin/sh\nexit 99\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(shadow), prepend=os.pathsep)
    monkeypatch.setattr(
        version_check, "TOOL_PATH", f"{approved}:{version_check.TOOL_PATH}"
    )
    monkeypatch.setattr(version_check, "REPO_URL", url)
    monkeypatch.setattr(version_heal, "_TEMP_ROOT", str(root))
    monkeypatch.chdir(root)
    restart = Mock()
    shell = Mock()
    monkeypatch.setattr(version_heal, "restart", restart)
    monkeypatch.setattr(cli_build, "script", Mock(return_value="exec uv --version"))
    monkeypatch.setattr(transport, "shell", shell)
    installed = f"tbhbdev 0 ({head})"

    assert version_heal.check(CONFIG, HOST, OLD_LINE, installed)
    restart.assert_called_once()
    shell.assert_not_called()


def test_versions_match_without_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    unexpected = Mock(side_effect=AssertionError("unexpected sync"))
    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(transport, "shell", unexpected)
    assert version_heal.check(CONFIG, HOST, OLD_LINE, OLD_LINE) is None
    unexpected.assert_not_called()


@pytest.mark.parametrize("after", [NEW_LINE, OLD_LINE])
def test_heal_checks_main_when_driver_and_host_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], after: str
) -> None:
    shell = Mock(side_effect=[OLD_LINE, SUMMARY, after])
    monkeypatch.setattr(version_heal.version, "line", lambda: OLD_LINE)
    run = Mock(side_effect=[ORIGIN, subprocess.CompletedProcess(INSTALL, 0, SUMMARY)])
    monkeypatch.setattr(version_heal.subprocess, "run", run)
    monkeypatch.setattr(version_heal, "restart", Mock())
    monkeypatch.setattr(version_heal.transport, "shell", shell)
    status = version_heal.heal_hosts(CONFIG, [HOST.name])
    assert status == (0 if after == NEW_LINE else 1)
    assert HEAD in shell.call_args_list[1].args[2]
    assert after in capsys.readouterr().out


@pytest.mark.parametrize("host_after", [NEW_LINE, f"tbhbdev 0 ({'c' * 40})"])
@pytest.mark.usefixtures("isolated_home")
def test_host_sync_retries_review_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host_after: str,
) -> None:
    run = Mock(return_value=ORIGIN)
    shell = Mock(
        side_effect=[
            OLD_LINE,
            SUMMARY,
            "0 4 12582912 6291456 0 0 - -",
            f"{host_after}\n{HEAD}\trefs/heads/topic\n",
        ]
    )
    stream = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(version, "line", lambda: NEW_LINE)
    monkeypatch.setattr(transport, "shell", shell)
    monkeypatch.setattr(transport, "stream", stream)
    monkeypatch.setattr(transport, "call", Mock())
    monkeypatch.setattr(startup, "poll_launch", Mock(return_value=0))
    monkeypatch.setattr(agent_settings, "load", lambda: CONFIG)
    prompt = tmp_path / "brief.md"
    prompt.write_text("Review the example.\n")
    argv = execution_arguments(prompt, "--profile", "review", "--ref", "topic")
    assert run_agent(argv) == (0 if host_after == NEW_LINE else 1)
    run.assert_called_once()
    assert run.call_args.args == (GIT,)
    assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    probe, install, limits_call, preflight = [c.args for c in shell.call_args_list]
    assert "--version" in probe[2]
    assert "# tbhbdev-limits" in limits_call[2]
    assert "ls-remote" in preflight[2]
    assert install[:2] == (CONFIG, HOST)
    command = install[2]
    assert command.startswith(remote_shell.PREFIX)
    assert cli_build.script(HEAD, URL) in command
    assert "--force" not in command
    err = capsys.readouterr().err
    assert err.splitlines()[0] == f"version heal: {SUMMARY}"
    if host_after == NEW_LINE:
        stream.assert_called_once()
        assert metadata.load(CONFIG, "sample").request.commit == HEAD
    else:
        stream.assert_not_called()
        assert (
            f"version mismatch: driver {NEW_LINE!r}; host {OLD_LINE!r} (after one sync)"
            in err
        )


@pytest.mark.parametrize("installed", [NEW_LINE, "tbhbdev 0 (unknown)"])
def test_driver_sync_execs_the_same_argv_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installed: str,
) -> None:
    argv = ["/example/bin/tbhbdev", "alpha", "agent", "exec", "sample", "brief.md"]
    original = f"version mismatch: driver {OLD_LINE!r}; host {installed!r}"
    run = Mock(side_effect=[ORIGIN, subprocess.CompletedProcess(INSTALL, 0, SUMMARY)])
    shell = Mock(return_value=SUMMARY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path))
    for key, value in NETWORK_ENV.items():
        monkeypatch.setenv(key, value)

    def execv(path: Path, args: list[str]) -> None:
        assert path == LIVE
        assert args == [str(LIVE), *argv[1:]]
        assert os.environ[RETRY] == original
        with pytest.raises(Invalid) as caught:
            version_heal.check(CONFIG, HOST, NEW_LINE, OLD_LINE)
        assert str(caught.value) == f"{original} (after one sync)"
        raise SystemExit(1)

    restart = Mock(side_effect=execv)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(os, "execv", restart)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(transport, "shell", shell)
    with pytest.raises(SystemExit):
        version_heal.check(CONFIG, HOST, OLD_LINE, installed)
    restart.assert_called_once()
    assert [call.args[0] for call in run.call_args_list] == [GIT, INSTALL]
    origin_call, install_call = [call.kwargs for call in run.call_args_list]
    assert Path(install_call["cwd"]) != tmp_path
    assert install_call["env"] == origin_call["env"]
    assert "UV_TOOL_BIN_DIR" not in install_call["env"]
    assert {key: install_call["env"][key] for key in NETWORK_ENV} == NETWORK_ENV
    assert run.call_args.kwargs["check"] is False
    assert run.call_args.kwargs["stderr"] == subprocess.DEVNULL
    assert RETRY not in os.environ
    log = capsys.readouterr().err.splitlines()
    assert log[-1] == f"version heal: {SUMMARY}"
    if installed == NEW_LINE:
        assert len(log) == 1
        shell.assert_not_called()
    else:
        assert log[0] == f"version heal: {SUMMARY}"
        shell.assert_called_once()


def test_matching_retry_consumes_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RETRY, "original mismatch")
    assert version_heal.check(CONFIG, HOST, NEW_LINE, NEW_LINE) is None
    assert RETRY not in os.environ


def test_empty_brief_is_refused_after_driver_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.touch()
    args = argparse.Namespace(
        name="sample",
        prompt_file=prompt,
        fetch_ref=[],
        profile="build",
        start_ref=None,
        ref=None,
    )
    assert checked_prompt(args) == ""
    monkeypatch.setenv(RETRY, "original mismatch")
    with pytest.raises(Invalid, match="brief is empty after the driver restarted"):
        checked_prompt(args)


def test_module_restart_uses_switched_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = ["alpha", "agent", "exec"]
    monkeypatch.setattr(sys, "argv", ["/example/tbhbdev/__main__.py", *args])
    restart = Mock(side_effect=FileNotFoundError("Python missing"))
    monkeypatch.setattr(os, "execv", restart)
    with pytest.raises(FileNotFoundError, match="Python missing"):
        version_heal.restart("original mismatch")
    restart.assert_called_once_with(LIVE, [str(LIVE), *args])
    assert RETRY not in os.environ


@pytest.mark.parametrize(
    ("local", "installed", "failure"),
    [
        (OLD_LINE, NEW_LINE, FileNotFoundError("uv missing")),
        (NEW_LINE, OLD_LINE, transport.TransportError("SSH failed")),
    ],
)
def test_sync_failure_preserves_the_mismatch(
    monkeypatch: pytest.MonkeyPatch, local: str, installed: str, failure: OSError
) -> None:
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=[ORIGIN, failure]))
    monkeypatch.setattr(transport, "shell", Mock(side_effect=failure))
    with pytest.raises(Invalid) as caught:
        version_heal.check(CONFIG, HOST, local, installed)
    assert str(caught.value) == (
        f"version mismatch: driver {local!r}; host {installed!r}"
        f" (version sync failed: {failure})"
    )


@pytest.mark.parametrize(
    ("origin", "installed"),
    [
        (OSError("Git missing"), OLD_LINE),
        (subprocess.CompletedProcess(GIT, 1, ""), OLD_LINE),
        (subprocess.CompletedProcess(GIT, 0, "garbled"), OLD_LINE),
        (ORIGIN, f"tbhbdev 1 ({HEAD})"),
    ],
)
def test_no_install_preserves_the_mismatch(
    monkeypatch: pytest.MonkeyPatch, origin: object, installed: str
) -> None:
    run = Mock(side_effect=[origin])
    shell = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(transport, "shell", shell)
    with pytest.raises(Invalid) as caught:
        version_heal.check(CONFIG, HOST, NEW_LINE, installed)
    assert (
        str(caught.value)
        == f"version mismatch: driver {NEW_LINE!r}; host {installed!r}"
    )
    run.assert_called_once()
    shell.assert_not_called()


def test_second_mismatch_after_sync_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Mock(return_value=ORIGIN))
    shell = Mock(return_value=f"{SUMMARY}\n")
    monkeypatch.setattr(transport, "shell", shell)
    original = version_heal.check(CONFIG, HOST, NEW_LINE, OLD_LINE)
    assert original is not None
    shell.reset_mock()
    with pytest.raises(Invalid) as caught:
        version_heal.check(CONFIG, HOST, NEW_LINE, OLD_LINE)
    assert str(caught.value) == f"{original} (after one sync)"
    shell.assert_not_called()


def test_failed_remote_build_reports_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = f"{OLD} {OLD} failed 0.1s"
    shell = Mock(return_value=f"{summary}\ninstall failed\n")
    monkeypatch.setattr(subprocess, "run", Mock(return_value=ORIGIN))
    monkeypatch.setattr(transport, "shell", shell)

    with pytest.raises(Invalid, match=summary):
        version_heal.check(CONFIG, HOST, NEW_LINE, OLD_LINE)


@pytest.mark.usefixtures("isolated_home")
def test_sync_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unexpected = Mock(side_effect=AssertionError("unexpected call"))
    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(transport, "shell", unexpected)
    with pytest.raises(TimeoutError, match="launch deadline"):
        version_heal.sync(CONFIG, HOST, NEW_LINE, OLD_LINE, deadline=0)
    monkeypatch.setenv(RETRY, "original mismatch")
    monkeypatch.setattr(subprocess, "run", Mock(return_value=ORIGIN))
    monkeypatch.setattr(version_heal, "restart", unexpected)
    with pytest.raises(OSError, match="restart"):
        version_heal.sync(CONFIG, HOST, OLD_LINE, NEW_LINE)
    unexpected.assert_not_called()
    monkeypatch.delenv(RETRY)
    monkeypatch.setattr(version, "line", lambda: NEW_LINE)
    monkeypatch.setattr(agent_settings, "load", lambda: CONFIG)
    for attr in ("stream", "call"):
        monkeypatch.setattr(transport, attr, Mock())
    monkeypatch.setattr(startup, "poll_launch", Mock(return_value=0))
    replies = [NEW_LINE, "0 4 12582912 6291456 0 0 - -", OLD_LINE, SUMMARY, NEW_LINE]
    monkeypatch.setattr(transport, "shell", Mock(side_effect=replies))
    prompt = tmp_path / "brief.md"
    prompt.write_text("Build.\n")
    assert run_agent(execution_arguments(prompt)) == 0
