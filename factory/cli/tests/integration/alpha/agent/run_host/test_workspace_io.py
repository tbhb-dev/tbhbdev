"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tomllib
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_host_example import (
    constant,
    local_mirror_fetches,
    make_run,
    preparation_failure,
    repository,
)
from run_host_fixtures import (
    HOST_FIXTURES,
    preparation,
)

from tbhbdev.alpha.agent import (
    credentials,
    push,
    runfiles,
    worktree_checkout,
    worktree_mirror,
)
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
    RunRequest,
    Settings,
)
from tbhbdev.alpha.agent.run_host import bazel_cache as host_bazel_cache
from tbhbdev.alpha.agent.run_host import environment as host_environment
from tbhbdev.alpha.agent.run_host import paths as host_paths
from tbhbdev.alpha.agent.run_host import preflight as host_preflight
from tbhbdev.alpha.agent.run_host import state
from tbhbdev.alpha.agent.run_host import workspace as host_workspace
from tbhbdev.alpha.install.hooks import stop_hooks as stop

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


@pytest.mark.parametrize(
    "step", ["credential delivery", "audit directory", "codex auth copy"]
)
@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (OSError, "[redacted]"),
        (ValueError, "invalid value"),
        (Invalid, "[redacted]"),
        (subprocess.CalledProcessError, "[redacted]"),
    ],
)
def test_workspace_failure_names_its_step_and_safe_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    error: type[Exception],
    cause: str,
) -> None:
    monkeypatch.setenv("EXAMPLE_CREDENTIAL", "sensitive-exception")
    monkeypatch.setenv("EXAMPLE_STDERR", "sensitive-stderr")
    run = make_run(tmp_path)
    monkeypatch.setattr(credentials, "deliver", constant(preparation(run).delivery))
    monkeypatch.setattr(worktree_checkout, "create", constant(run.home / "workspace"))

    fail = partial(preparation_failure, error)

    if step == "credential delivery":
        monkeypatch.setattr(credentials, "deliver", fail)
    elif step == "audit directory":
        monkeypatch.setattr(runfiles, "private_dir", fail)
    else:
        monkeypatch.setattr(credentials, "auth_copy", fail)
    with pytest.raises(Invalid) as caught:
        host_workspace.prepare_workspace(run)
    assert str(caught.value) == f"run preparation failed at {step}: {cause}"
    assert "sensitive" not in str(caught.value)


def with_identity_cli(run: state.Run, *, ready: bool) -> state.Run:
    tools = run.home / ".local/bin"
    tools.mkdir(parents=True)
    cli = tools / "tbhbdev"
    check = '[ "$*" = "alpha identity trailer --dry-run" ]' if ready else "exit 1"
    cli.write_text(f"#!/bin/sh\n{check}\n")
    cli.chmod(0o755)
    return dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(run.settings.sandbox, host_path=(str(tools),)),
        ),
    )


def test_hook_probe_refuses_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = with_identity_cli(make_run(tmp_path), ready=False)

    def forbidden(*_args: object) -> str:
        pytest.fail("hook failure must precede fetch and credential delivery")

    monkeypatch.setattr(worktree_mirror, "fetch_commit", forbidden)
    monkeypatch.setattr(credentials, "deliver", forbidden)
    with pytest.raises(Invalid) as caught:
        host_workspace.prepare_workspace(run)
    assert str(caught.value) == (
        "run preparation failed at hook verification: "
        "tbhbdev alpha identity trailer --dry-run failed in the run environment"
    )
    assert not Path(host_paths.planned_paths(run).worktree).exists()


@pytest.mark.parametrize("harness", Harness)
def test_prepare_uses_one_worktree_and_private_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        profile="review",
        commit="0" * 40,
        timeout=900,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example" if harness is Harness.CLAUDE else None,
    )
    _, clone = repository(run.home / "Code" / "github.com" / "tbhb-dev")
    clone.rename(clone.parent / "example-repo")
    clone = clone.parent / "example-repo"
    local_mirror_fetches(monkeypatch, clone.parent / "origin")
    commit = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()
    run = dataclasses.replace(
        run, request=dataclasses.replace(run.request, commit=commit)
    )
    run = with_identity_cli(run, ready=True)
    value, _ = host_preflight.resolve_harness(run, host_paths.planned_paths(run))
    expected_path = value.tool_path if harness is Harness.CODEX else value.env["PATH"]
    assert host_environment.hook_environment(value)["PATH"] == expected_path
    if harness is Harness.CLAUDE:
        assert host_environment.hook_environment(value) == value.env
    source = runfiles.private_dir(run.home / ".codex") / "auth.json"
    source.write_text("example-auth")
    (source.parent / "hooks.json").write_text('{"hooks":{"SessionStart":[]}}')
    copy = credentials.auth_copy

    def auth_copy(source_path: Path, destination: Path) -> str | None:
        assert source_path == run.home / ".codex" / "auth.json"
        assert (
            destination == Path(host_paths.planned_paths(run).codex_home) / "auth.json"
        )
        return copy(source_path, destination)

    monkeypatch.setattr(credentials, "auth_copy", auth_copy)
    deliver = credentials.deliver

    def after_git_shape(
        request: RunRequest, settings: Settings, tmpdir: Path
    ) -> credentials.Delivery:
        workspace = clone / ".worktrees" / "agent-example"
        assert (workspace / "tracked").is_file()
        assert (workspace / ".git/hooks/pre-commit").is_file()
        assert (workspace / ".git/config.worktree").read_text() == ""
        assert (workspace / ".git/commondir").read_text() == ".\n"
        assert list((workspace / ".git/modules").iterdir()) == []
        return deliver(request, settings, tmpdir)

    monkeypatch.setattr(credentials, "deliver", after_git_shape)
    result = host_workspace.prepare_workspace(run)
    assert result.paths.home == str(run.home)
    assert result.paths.worktree == str(clone / ".worktrees" / "agent-example")
    assert result.paths.tmpdir == str(run.tmpdir)
    assert result.paths.codex_home == host_paths.planned_paths(run).codex_home
    assert result.paths.signing_socket is None
    assert result.paths.audit_dir == str(run.home / run.settings.sandbox.audit_dir)
    assert Path(result.paths.audit_dir).is_dir()
    assert result.auth_before == ("example-auth" if harness is Harness.CODEX else None)
    assert (run.tmpdir / "gh" / "hosts.yml").is_file()
    copied_auth = Path(result.paths.codex_home) / "auth.json"
    assert copied_auth.exists() is (harness is Harness.CODEX)
    if harness is Harness.CODEX:
        assert copied_auth.read_text() == "example-auth"
        assert_stop_config(copied_auth.parent)


def assert_stop_config(home: Path) -> None:
    path = home / "hooks.json"
    assert json.loads(path.read_text()) == {"hooks": stop("codex")}
    config = tomllib.loads(path.with_name("config.toml").read_text())
    assert config["hooks"]["state"].keys() == {f"{path}:stop:0:0"}


def otel_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    endpoint: str = "https://otlp.example.net:4318",
) -> state.Run:
    run = make_run(
        tmp_path,
        harness=harness.value,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example" if harness is Harness.CLAUDE else None,
    )
    settings = dataclasses.replace(run.settings, otel_endpoint=endpoint)
    run = dataclasses.replace(run, settings=settings)
    runfiles.private_dir(run.home / ".codex")
    monkeypatch.setattr(credentials, "deliver", constant(preparation(run).delivery))
    monkeypatch.setattr(worktree_checkout, "create", constant(run.home / "workspace"))
    monkeypatch.setattr(push, "head", constant("a" * 40))
    monkeypatch.setattr(credentials, "auth_copy", constant(None))
    return run


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_codex_run_config_holds_the_otel_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    endpoint = f"{scheme}://otlp.example.net:4318"
    run = otel_run(tmp_path, monkeypatch, Harness.CODEX, endpoint)
    result = host_workspace.prepare_workspace(run)
    config_path = Path(result.paths.codex_home) / "config.toml"
    # The macOS filesystem ignores case, so name the file as written.
    assert "config.toml" in [p.name for p in config_path.parent.iterdir()]
    data = tomllib.loads(config_path.read_text())
    assert data.pop("hooks")["state"]
    assert data == {
        "otel": {
            "trace_exporter": {
                "otlp-http": {
                    "endpoint": f"{endpoint}/v1/traces",
                    "protocol": "binary",
                }
            },
            "metrics_exporter": {
                "otlp-http": {
                    "endpoint": f"{endpoint}/v1/metrics",
                    "protocol": "binary",
                }
            },
            "log_user_prompt": False,
        }
    }
    assert "headers" not in config_path.read_text()
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_codex_run_without_an_endpoint_writes_stop_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = otel_run(tmp_path, monkeypatch, Harness.CODEX)
    result = host_workspace.prepare_workspace(run)
    assert_stop_config(Path(result.paths.codex_home))


def test_claude_run_writes_no_codex_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = otel_run(tmp_path, monkeypatch, Harness.CLAUDE)
    result = host_workspace.prepare_workspace(run)
    assert not (Path(result.paths.codex_home) / "config.toml").exists()


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_codex_config_failure_names_its_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    monkeypatch.setenv("EXAMPLE_CREDENTIAL", "sensitive-exception")
    run = otel_run(tmp_path, monkeypatch, Harness.CODEX)
    monkeypatch.setattr(runfiles, "atomic", partial(preparation_failure, error))
    with pytest.raises(Invalid) as caught:
        host_workspace.prepare_workspace(run)
    cause = "[redacted]" if error is OSError else "invalid value"
    assert str(caught.value) == f"run preparation failed at codex config: {cause}"


@pytest.mark.parametrize(("no_commit", "start"), [(False, "a" * 40), (True, None)])
def test_build_preparation_has_the_run_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_commit: bool, start: str | None
) -> None:
    run = make_run(tmp_path, no_commit=no_commit)

    def fetch_token(request: RunRequest, settings: Settings) -> str:
        assert (request, settings) == (run.request, run.settings)
        return "example-credential"

    def bazel_bases(home: Path, workspace: str) -> tuple[Path, ...]:
        assert (home, workspace) == (run.home, str(run.home / "worktree"))
        return (run.home / "base",)

    def create(
        _clone: Path,
        _name: str,
        _value: RunRequest,
        token_source: Callable[[], str | None],
        *,
        hook_env: Mapping[str, str],
    ) -> Path:
        assert not [name for name in hook_env if name.startswith("GIT_")]
        assert token_source() == "example-credential"
        return runfiles.private_dir(run.home / "worktree")

    monkeypatch.setattr(worktree_checkout, "create", create)
    baseline = Mock(return_value=start)
    monkeypatch.setattr(push, "head", baseline)
    monkeypatch.setattr(host_bazel_cache, "bazel_bases", bazel_bases)
    monkeypatch.setattr(credentials, "fetch_token", fetch_token)
    result = host_workspace.prepare_workspace(run)
    assert result.paths.signing_socket == str(run.tmpdir / "ssh.sock")
    assert result.delivery.signing == "example-signing"
    assert result.bases == (run.home / "base",)
    assert result.start == start
    assert baseline.call_count == int(not no_commit)
