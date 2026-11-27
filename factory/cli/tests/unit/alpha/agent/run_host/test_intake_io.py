"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest
from agent_host_example import (
    constant,
    make_run,
    run_request,
)
from test_alpha_agent_settings import EXAMPLE

from tbhbdev.alpha.agent import (
    runfiles,
    settings,
)
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import (
    Invalid,
)
from tbhbdev.alpha.agent.run_host import dispatcher as host_dispatcher
from tbhbdev.alpha.agent.run_host import execution as host_execution
from tbhbdev.alpha.agent.run_host import intake as host_intake
from tbhbdev.alpha.agent.run_host import state

pytestmark = pytest.mark.usefixtures(
    "empty_bazel_listing", "fake_secrets", "portable_run_protocol", "stub_push"
)


@pytest.mark.parametrize("source", ["file", "stdin", "recorded"])
def test_hidden_dispatch_reads_request_and_restores_handlers(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    config_file.write_text(EXAMPLE)
    expected_settings = settings.load()
    files = runfiles.RunFiles(
        Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs", "example"
    )
    files.write("prompt", "example task")
    files.write("json", "driver metadata")
    value = run_request(harness="claude", claude_profile="Primary", reason="example")
    raw = json.dumps(request.to_dict(value))
    arguments = ["run-host", "example", "--host", "example-linux"]
    if source == "recorded":
        files.write("request.json", raw)
    elif source == "stdin":
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        arguments.insert(2, "-")
    else:
        path = tmp_path / "received.json"
        path.write_text(raw)
        arguments.insert(2, str(path))
        prompt = tmp_path / "prompt"
        prompt.write_text("example task")
        arguments += ["--prompt", str(prompt)]
    before = {
        s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    }

    def execute(
        run: state.Run,
        text: str,
        *,
        preflight_only: bool = False,
    ) -> int:
        assert preflight_only is False
        assert run.settings == expected_settings
        assert run.request.claude_profile == "primary"
        assert run.files.root == files.root
        assert files.path("run.lock").is_file()
        assert run.home == Path.home()
        assert text == "example task"
        assert run.tmpdir.is_dir()
        assert run.tmpdir.name == "tools"
        assert 0 < run.started <= time.time()
        assert 0 < run.deadline - time.monotonic() <= 120
        for number in before:
            assert signal.getsignal(number) is host_dispatcher.interrupt
        return 9

    monkeypatch.setattr(host_execution, "execute", execute)
    assert host_dispatcher.main(arguments[1:], "example agent run-host") == 9
    assert {s: signal.getsignal(s) for s in before} == before
    assert files.path("json").read_text() == "driver metadata"


def test_host_defaults_use_fake_home_and_configured_machine(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file.write_text(EXAMPLE)
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(socket, "gethostname", lambda: "example-linux")
    root = Path.home() / ".local" / "state" / "tbhbdev" / "agent-runs"
    files = runfiles.RunFiles(root, "example")
    files.receive(run_request())
    files.write("prompt", "example task")

    def execute(
        run: state.Run,
        text: str,
        *,
        preflight_only: bool = False,
    ) -> int:
        assert preflight_only is False
        assert run.files.root == root
        assert run.host.name == "example-linux"
        assert text == "example task"
        return 37

    monkeypatch.setattr(host_execution, "execute", execute)
    assert host_dispatcher.main(["example"], "example run-host") == 37


def test_host_passes_configured_full_access_authorization(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file.write_text(EXAMPLE)
    account_path = os.environ["PATH"]
    root = Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs"
    files = runfiles.RunFiles(root, "example")
    files.receive(
        run_request(
            unsandboxed=True,
            authorized_by="operator-chat-2026-11-09",
            reason="approval",
        )
    )
    files.write("prompt", "example task")

    def execute(
        run: state.Run,
        _text: str,
        *,
        preflight_only: bool = False,
    ) -> int:
        assert preflight_only is False
        assert os.environ["PATH"] == account_path
        assert run.request.unsandboxed is True
        assert run.request.authorized_by == "operator-chat-2026-11-09"
        return 37

    monkeypatch.setattr(host_execution, "execute", execute)
    assert (
        host_dispatcher.main(["example", "--host", "example-linux"], "example run-host")
        == 37
    )


def test_main_defaults_to_the_current_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    def load(args: object, _tmpdir: Path) -> tuple[state.Run, str]:
        assert isinstance(args, argparse.Namespace)
        assert args.host == "example-linux"
        return run, "task"

    monkeypatch.setattr(socket, "gethostname", constant("example-linux"))
    monkeypatch.setattr(host_intake, "load", load)
    monkeypatch.setattr(host_execution, "execute", constant(7))
    assert host_dispatcher.main(["example"], "example") == 7


def test_main_keeps_account_path_until_request_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted"))

    def load(_args: object, _tmpdir: Path) -> None:
        assert os.environ["PATH"] == str(tmp_path / "untrusted")
        message = "loading probe"
        raise Invalid(message)

    monkeypatch.setattr(host_intake, "load", load)
    assert host_dispatcher.main(["example"], "example") == 1
