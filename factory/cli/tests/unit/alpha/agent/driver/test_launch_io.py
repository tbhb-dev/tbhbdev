"""Exercise coordinator launch through fake tools and run records."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import shlex
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent
from agent_driver_example import (
    Remote,
    setup,
)
from agent_example import MARKER
from driver_scenarios import (
    COMMIT,
    CONFIG,
    arguments,
    command,
    disable_host_sandbox_setting,
    launch,
)

from tbhbdev import version
from tbhbdev.alpha.agent import model, transport
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.decisions import remote_host, remote_run, remote_shell, request
from tbhbdev.alpha.agent.driver import launch as coordinator
from tbhbdev.alpha.agent.driver import (
    metadata,
    version_heal,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from tbhbdev.alpha.agent.driver.decisions.metadata import Run


def test_default_launch_sends_unsandboxed_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_settings, "load", lambda: CONFIG)
    assert launch(remote) == 0
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["unsandboxed"] is True
    assert wire["authorized_by"] is None


def test_launch_stream_and_metadata(
    remote: Remote, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    schema = remote.root / "schema.json"
    schema.write_text('{"type":"object"}')
    assert (
        launch(
            remote,
            "--schema",
            str(schema),
            "--class",
            "test",
            "--variant",
            "one",
            "--role",
            "worker",
            "--no-expect-commit",
            "--fetch-ref",
            "refs/pull/391/head",
            "--fetch-ref",
            "refs/heads/fix-392",
        )
        == 0
    )
    assert capsys.readouterr() == (
        "sample: launched host=example-linux account=agent\n",
        "",
    )
    data = json.loads((remote.state / "sample.request.json").read_text())
    parsed = request.parse(data, host_allows_unsandboxed=True)
    assert request.to_dict(parsed) == data
    assert (parsed.class_, parsed.variant, parsed.role) == ("test", "one", "worker")
    assert parsed.commit is None
    assert parsed.account == "agent"
    assert parsed.timeout is None
    assert parsed.startup_deadline_seconds == 120
    assert parsed.fetch_refs == ("refs/pull/391/head", "refs/heads/fix-392")
    assert parsed.no_commit
    assert (remote.state / "sample.prompt").read_text() == remote.prompt.read_text()
    assert (remote.state / "sample.schema.json").read_text() == schema.read_text()
    assert remote.state.stat().st_mode & 0o777 == 0o700
    for suffix in (".request.json", ".prompt", ".schema.json"):
        assert (remote.state / ("sample" + suffix)).stat().st_mode & 0o777 == 0o600
    meta = remote.local / "tbhbdev/agent-runs/sample.json"
    record = json.loads(meta.read_text())
    assert record == {
        **data,
        "host": "example-linux",
        "started": record["started"],
        "owner": record["owner"],
        "launcher": "tools",
        "launcher_session": "session-1",
        "cleaning": False,
        "ignore_limits": False,
    }
    assert len(record["owner"]) == 32
    assert (remote.state / "sample.owner").read_text() == record["owner"] + "\n"
    assert record["started"].endswith("+00:00")
    assert meta.stat().st_mode & 0o777 == 0o600
    assert meta.parent.stat().st_mode & 0o777 == 0o700
    calls = remote.records("ssh")
    assert len(calls) == 4
    owner_line, archive = base64.b64decode(str(calls[1]["stdin"])).split(b"\n", 1)
    assert owner_line.decode() == record["owner"]
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        assert stream.getnames() == [
            "sample.request.json",
            "sample.prompt",
            "sample.schema.json",
        ]
    for tool in ("ssh", "tmux"):
        assert MARKER not in json.dumps(remote.records(tool))
    for tool in ("ssh", "tmux"):
        argv = [record["args"] for record in remote.records(tool)]
        assert record["owner"] not in json.dumps(argv)
    assert MARKER not in meta.read_text()
    tmux = remote.records("tmux")[0]["args"]
    assert tmux == shlex.split(
        f"-S {remote.root}/sockets/agents -f /dev/null has-session -t =ag-sample"
    )


STEER_ERROR = "exec --steer requires a Codex build without --schema"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (("--ref", "main"), "--ref applies only to a review run"),
        (("--repo", "bare"), "must be org/repo"),
        (("--host", "missing"), "host 'missing' isn't configured"),
        (("--account", "missing"), "account 'missing' isn't one"),
        (("--claude-profile", "primary"), "applies only to a claude run"),
        (
            (
                "--harness",
                "claude",
                "--reason",
                "example",
                "--claude-profile",
                "Missing",
            ),
            "profile 'Missing' isn't defined",
        ),
        (("--harness", "claude"), "reason is required"),
        (("--unsandboxed", "bad"), "request authorized_by must be"),
        (("--harness", "agy"), "agy requires a gemini-*"),
        (("--model", "bad model"), "model must match"),
        (("--effort", "bad effort"), "effort must match"),
        (("--steer", "--harness", "claude"), STEER_ERROR),
        (("--steer", "--profile", "review"), STEER_ERROR),
        (("--steer", "--schema", "schema.json"), STEER_ERROR),
    ],
)
def test_checks_before_ssh(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    message: str,
) -> None:
    assert launch(remote, *options) == 1
    assert message in capsys.readouterr().err
    assert remote.records("ssh") == []


def test_reused_name(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    before = remote.records("ssh")
    assert launch(remote) == 1
    assert (
        "run metadata already exists. Run `tbhbdev alpha agent clean --force sample`"
        in capsys.readouterr().err
    )
    assert remote.records("ssh") == before


@pytest.mark.parametrize("name", ["../bad", "-bad", "a b", "a" * 65, ""])
def test_bad_name(
    remote: Remote, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    args = arguments(remote)
    args[-2] = name
    assert run_agent(args) in (1, 2)
    assert remote.records("ssh") == []
    assert capsys.readouterr().err


def test_version_mismatch(
    remote: Remote, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(version_heal, "read_origin", Mock(return_value=(1, "")))
    remote.configure(version="tbhbdev example-other")
    assert launch(remote) == 1
    message = capsys.readouterr().err
    assert "version mismatch" in message
    assert version.line() in message
    assert "tbhbdev example-other" in message
    assert len(remote.records("ssh")) == 1


def test_review_without_ref_starts_nothing(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--profile", "review") == 1
    assert "a review run needs --ref" in capsys.readouterr().err
    assert remote.records("ssh") == []


def test_review_check_refuses_empty_ref(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--profile", "review", "--ref", "") == 1
    assert "a review run needs --ref" in capsys.readouterr().err
    assert remote.records("ssh") == []
    assert not metadata.metadata_path("sample").exists()


def test_a_review_checks_and_saves_inside_the_launch_lock(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_lock = metadata.launch_lock
    real_save = metadata.save

    @contextlib.contextmanager
    def lock() -> Generator[None]:
        events.append("enter")
        with real_lock():
            yield
        events.append("exit")

    def refuse(*_args: object) -> None:
        events.append("check")

    def save(run: Run) -> None:
        events.append("save")
        real_save(run)

    monkeypatch.setattr(coordinator, "launch_lock", lock)
    monkeypatch.setattr(coordinator, "refuse_live_duplicate", refuse)
    monkeypatch.setattr(coordinator, "save", save)
    assert launch(remote, "--profile", "review", "--ref", COMMIT) == 0
    assert events == ["enter", "check", "save", "exit"]


@pytest.mark.parametrize("flag", ["--expect-commit", "--no-expect-commit"])
def test_exec_refuses_commit_flags_on_a_review(
    remote: Remote, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    assert launch(remote, "--profile", "review", "--ref", COMMIT, flag) == 1
    assert (
        "request expect_commit applies only to a build run" in capsys.readouterr().err
    )
    assert remote.records("ssh") == []


@pytest.mark.parametrize(
    ("flag", "required"), [("--expect-commit", True), ("--no-expect-commit", False)]
)
def test_exec_accepts_explicit_commit_policy(
    remote: Remote, flag: str, *, required: bool
) -> None:
    assert launch(remote, flag) == 0
    data = json.loads((remote.state / "sample.request.json").read_text())
    assert data["expect_commit"] is required


@pytest.mark.parametrize("ref", ["topic", "refs/pull/2/head", COMMIT])
def test_review_ref(remote: Remote, ref: str) -> None:
    remote.configure(refs=f"{COMMIT}\t{ref}\n")
    assert launch(remote, "--profile", "review", "--ref", ref) == 0
    assert (
        json.loads((remote.state / "sample.request.json").read_text())["commit"]
        == COMMIT
    )
    calls = remote.records("ssh")
    assert "--version" in str(calls[0]["args"])
    git = remote.records("git")
    if ref == COMMIT:
        assert git == []
    else:
        assert git == [
            {
                "args": [
                    *shlex.split(remote_shell.GIT_FLAGS)[1:],
                    "ls-remote",
                    "--exit-code",
                    "--",
                    "git@github.com:example-org/example-repo.git",
                    ref,
                    ref + "^{}",
                ],
                "stdin": "",
            }
        ]


@pytest.mark.parametrize("profile", [None, "Primary", "PRIMARY"])
def test_claude_case(
    remote: Remote, profile: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    options = () if profile is None else ("--claude-profile", profile)
    assert (
        launch(remote, "--harness", "claude", "--reason", "example-gap", *options) == 0
    )
    assert (
        json.loads((remote.state / "sample.request.json").read_text())["claude_profile"]
        == "primary"
    )
    assert command("send", "hello") == 1
    assert "Claude delivery is not built" in capsys.readouterr().err


def test_agy_review_uses_the_review_timeout(remote: Remote) -> None:
    remote.configure(refs=f"{COMMIT}\ttopic\n")
    options = ("--harness", "agy", "--model", "gemini-example")
    assert launch(remote, *options, "--profile", "review", "--ref", "topic") == 0
    record = json.loads((remote.state / "sample.request.json").read_text())
    assert record["timeout"] == 900


@pytest.mark.parametrize(
    ("options", "timeout"),
    [
        (("--ops", "--unsandboxed", "o/r#1"), None),
        (("--harness", "agy", "--model", "gemini-example"), 3600),
        (("--harness", "agy", "--allow-non-gemini", "--timeout", "1h"), 3600),
        (
            (
                *("--harness", "agy", "--model", "gemini-example"),
                *("--profile", "build", "--timeout", "5m"),
            ),
            300,
        ),
    ],
)
def test_request_options(
    remote: Remote, options: tuple[str, ...], timeout: int | None
) -> None:
    assert launch(remote, *options) == 0
    record = json.loads((remote.state / "sample.request.json").read_text())
    assert record["timeout"] == timeout
    if "--ops" in options:
        assert record["account"] == "ops"
        assert record["unsandboxed"] is True
        assert record["authorized_by"] == "o/r#1"
        assert "-l', 'ops'" in str(remote.records("ssh")[0]["args"])


def test_stream_transport_failure_never_kills(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    remote.configure(ssh=[None, {"rc": 255}], kill_rc="1\n")
    assert launch(remote) == 1
    assert "not owner" in capsys.readouterr().err
    assert not metadata.metadata_path("sample").exists()
    assert not (remote.state / "sample.rc").exists()


def test_exec_spends_one_startup_budget(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps: list[str] = []

    def shell(
        settings: model.Settings,
        host: model.Host,
        script: str,
        _stdin: transport.Stdin | None = None,
        *,
        timeout: float,
    ) -> str:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        if script == remote_host.limits():
            assert timeout == 110
            steps.append("limits")
            return "0.5 4 12582912 6291456 100 0 -"
        assert script == remote_host.installed(CONFIG.hosts[0])
        assert timeout == 100
        steps.append("preflight")
        return version.line() + "\n"

    def stream(
        settings: model.Settings,
        host: model.Host,
        name: str,
        *,
        inputs: transport.Inputs,
        timeout: float,
    ) -> None:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        assert name == "sample"
        assert timeout == 90
        assert inputs.owner == metadata.load(CONFIG, "sample").owner
        assert inputs.schema is None
        assert inputs.prompt.endswith("Carry out the example.\n")
        assert json.loads(inputs.request)["commit"] is None
        steps.append("stream")

    def call(
        settings: model.Settings,
        host: model.Host,
        argv: tuple[str, ...],
        stdin: transport.Stdin | None,
        *,
        timeout: float,
    ) -> str:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        assert stdin == transport.Stdin(metadata.load(CONFIG, "sample").owner)
        assert argv == remote_run.start("sample", CONFIG.sockets_dir, CONFIG.hosts[0])
        assert timeout == 80
        steps.append("start")
        return ""

    def read(
        settings: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        timeout: float,
    ) -> transport.Reading:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        assert name == ("sample", metadata.load(CONFIG, "sample").owner)
        assert timeout == 70
        steps.append("poll")
        return transport.Reading("running", None, launched=True, session=True, text="")

    monkeypatch.setattr(transport, "shell", shell)
    monkeypatch.setattr(transport, "stream", stream)
    monkeypatch.setattr(transport, "call", call)
    monkeypatch.setattr(transport, "read", read)
    assert launch(remote) == 0
    assert steps == ["limits", "preflight", "stream", "start", "poll"]


def test_explicit_codex_harness(remote: Remote) -> None:
    assert launch(remote, "--harness", "codex") == 0


def test_review_version_mismatch_precedes_clone_read(
    remote: Remote, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(version_heal, "read_origin", Mock(return_value=(1, "")))
    remote.configure(version="tbhbdev example-other")
    assert launch(remote, "--profile", "review", "--ref", "topic") == 1
    message = capsys.readouterr().err
    assert version.line() in message
    assert "tbhbdev example-other" in message
    assert remote.records("git") == []


@pytest.mark.parametrize("existing", ["session", ".rc", ".launched"])
def test_remote_name_collision_preserves_other_run(
    remote: Remote, capsys: pytest.CaptureFixture[str], existing: str
) -> None:
    if existing == "session":
        remote.configure(session=True)
    else:
        remote.file(existing, "other run\n")
    before = {p.name: p.read_bytes() for p in remote.state.iterdir()}
    assert launch(remote) == 1
    assert "collision" in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in remote.state.iterdir()} == before
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))
    assert not metadata.metadata_path("sample").exists()


@pytest.mark.parametrize("transferred", [True, False])
def test_transfer_error_checks_owner_before_retaining_metadata(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    transferred: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = transport.stream

    def fail(
        config: model.Settings,
        host: model.Host,
        name: str,
        *,
        inputs: transport.Inputs,
        timeout: float,
    ) -> None:
        if transferred:
            original(config, host, name, inputs=inputs, timeout=timeout)
        else:
            remote.configure(ssh=[{"rc": 255}])
        message = "lost transfer reply"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "stream", fail)
    assert launch(remote) == 75
    assert metadata.metadata_path("sample").exists()
    assert capsys.readouterr() == (
        "sample: launch unconfirmed host=example-linux phase=unreachable\n",
        "",
    )
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))


def test_lost_start_reply_keeps_unreachable_run_reachable(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = transport.call

    def lost_reply(
        config: model.Settings,
        host: model.Host,
        command: tuple[str, ...],
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        reply = original(config, host, command, stdin, timeout=timeout)
        if "new-session" in command[2]:
            remote.configure(ssh=[{"rc": 255}])
            message = "lost start reply"
            raise transport.TransportError(message)
        return reply

    with monkeypatch.context() as patch:
        patch.setattr(transport, "call", lost_reply)
        result = launch(remote)
    assert metadata.metadata_path("sample").exists()
    assert result == 75
    assert capsys.readouterr() == (
        "sample: launch unconfirmed host=example-linux phase=unreachable\n",
        "",
    )
    run = metadata.load(CONFIG, "sample")
    assert (remote.state / "sample.owner").read_text() == run.owner + "\n"
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))
    assert command("status") == 0
    assert "phase=unknown" in capsys.readouterr().out
    remote.file(".rc", "7\n")
    remote.configure(session=False)
    assert command("wait") == 7


def test_unsandboxed_new_launch_ignores_host_setting(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disable_host_sandbox_setting(monkeypatch)
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    assert capsys.readouterr().err == ""
    assert remote.records("ssh")
