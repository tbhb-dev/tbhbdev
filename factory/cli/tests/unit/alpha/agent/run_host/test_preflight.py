"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from agent_host_example import (
    constant,
    make_run,
)
from run_host_fixtures import (
    RESOLVE,
    preparation,
)

from tbhbdev.alpha.agent import (
    host_tools,
)
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
)
from tbhbdev.alpha.agent.run_host import harness as host_harness
from tbhbdev.alpha.agent.run_host import paths as host_paths
from tbhbdev.alpha.agent.run_host import preflight as host_preflight

pytestmark = pytest.mark.usefixtures(
    "empty_bazel_listing", "fake_secrets", "portable_run_protocol", "stub_push"
)


@pytest.mark.parametrize("missing", ["reviews_app_client_id", "reviews_app_pem"])
def test_preflight_refuses_review_without_app_references(
    tmp_path: Path, missing: str
) -> None:
    run = make_run(tmp_path, profile="review", commit="0" * 40)
    original = run.settings.credentials[0]
    references = dataclasses.replace(
        original,
        reviews_app_client_id=None
        if missing == "reviews_app_client_id"
        else original.reviews_app_client_id,
        reviews_app_pem=None
        if missing == "reviews_app_pem"
        else original.reviews_app_pem,
    )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, credentials=(references,))
    )
    with pytest.raises(Invalid) as caught:
        host_preflight.preflight(run)
    assert str(caught.value) == "reviews App credentials are not configured"


def test_preflight_checks_the_requested_accounts_app(tmp_path: Path) -> None:
    run = make_run(tmp_path, profile="review", commit="0" * 40)
    references = run.settings.credentials[0]
    other = dataclasses.replace(references, account="ops", reviews_app_pem=None)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(run.settings, credentials=(other, references)),
    )
    host_preflight.preflight(run)


def test_preflight_announces_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = make_run(
        tmp_path,
        authorized_by="tbhb-dev/example-repo#1",
    )
    host_preflight.preflight(run)
    expected = "unsandboxed full access authorized_by=tbhb-dev/example-repo#1\n"
    assert capsys.readouterr().err == expected
    assert run.files.path("log").read_text() == expected


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", ["build", "review"])
def test_unsandboxed_preflight_launches_every_harness(
    tmp_path: Path, harness: Harness, profile: str
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        profile=profile,
        commit="0" * 40 if profile == "review" else None,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="operator approval",
        timeout=900,
        unsandboxed=True,
        authorized_by="operator-chat-2026-11-15",
    )
    host_preflight.preflight(run)


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("configured", [False, True])
def test_unsandboxed_codex_uses_account_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool, configured: bool
) -> None:
    monkeypatch.setattr(host_tools, "resolve", RESOLVE)
    run = make_run(tmp_path, unsandboxed=True, authorized_by="operator-chat-2026-11-15")
    account_dirs = tuple(
        path
        for path in host_tools.account_path(str(run.home))
        if Path(path).is_relative_to(run.home)
    )
    monkeypatch.setattr(host_tools, "account_path", constant(account_dirs))
    directory = run.home / ".local/bin"
    directory.mkdir(parents=True)
    binary = directory / "codex"
    if not missing:
        binary.write_bytes(b"binary")
        binary.chmod(0o755)
    inherited_path = str(tmp_path / "empty") if configured else str(directory)
    monkeypatch.setenv("PATH", inherited_path)
    sandbox_path = dataclasses.replace(
        run.settings.sandbox.path, linux=(str(directory),)
    )
    sandbox = dataclasses.replace(run.settings.sandbox, path=sandbox_path)
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, sandbox=sandbox)
    )

    if missing:
        with pytest.raises(Invalid) as caught:
            host_preflight.resolve_harness(run, host_paths.planned_paths(run))
        search_path = ":".join(
            (inherited_path, *host_tools.account_path(str(run.home)))
        )
        assert (
            str(caught.value)
            == f"host executable codex was not found on PATH {search_path}"
        )
    else:
        value, executable = host_preflight.resolve_harness(
            run, host_paths.planned_paths(run)
        )
        assert executable == str(binary)
        assert value.env["PATH"].split(":")[0] == inherited_path
        assert str(directory) in value.env["PATH"].split(":")


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", ["build", "review"])
@pytest.mark.parametrize(
    "directory", [".local/bin", ".local/gh-bin", ".bun/bin", ".npm-global/bin"]
)
def test_full_access_finds_every_harness_in_the_run_accounts_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    profile: str,
    directory: str,
) -> None:
    monkeypatch.setattr(host_tools, "resolve", RESOLVE)
    run = make_run(
        tmp_path,
        unsandboxed=True,
        authorized_by=None,
        harness=harness.value,
        profile=profile,
        commit="0" * 40 if profile == "review" else None,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="test run",
        timeout=900,
    )
    name = harness.value
    binary = run.home / directory / name
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    binary.chmod(0o755)
    inherited = str(tmp_path / "empty")
    monkeypatch.setenv("PATH", inherited)
    value, executable = host_preflight.resolve_harness(
        run, host_paths.planned_paths(run)
    )
    assert executable == str(binary)
    assert value.env["PATH"] == ":".join(
        (inherited, *host_tools.account_path(str(run.home)))
    )
    assert not run.files.path("log").exists()


@pytest.mark.parametrize("harness", Harness)
def test_full_access_keeps_the_mise_shim_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    monkeypatch.setattr(host_tools, "resolve", RESOLVE)
    run = make_run(
        tmp_path,
        unsandboxed=True,
        harness=harness.value,
        timeout=900,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="test run",
    )
    directory = run.home / ".local/share/mise/shims"
    directory.mkdir(parents=True)
    name = harness.value
    binary = directory / name
    binary.write_bytes(b"binary")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))
    with pytest.raises(Invalid, match=f"host executable {name} is a mise shim"):
        host_preflight.resolve_harness(run, host_paths.planned_paths(run))


def test_full_access_is_available_behind_the_existing_gate(tmp_path: Path) -> None:
    run = make_run(
        tmp_path,
        unsandboxed=True,
        authorized_by="tbhb-dev/example-repo#1",
        reason="isolated host",
    )
    host_preflight.preflight(run)
    prepared = preparation(run)
    run.files.write("prompt", "probe")
    value, executable = host_preflight.resolve_harness(run, prepared.paths)
    command = host_harness.harness_command(run, prepared, value, executable)
    assert command[command.index("-s") + 1] == "danger-full-access"
    assert not any(arg.startswith("permissions.") for arg in command)
