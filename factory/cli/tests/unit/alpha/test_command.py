"""Tests for the alpha command registry and dispatch."""

from importlib import import_module
from unittest.mock import Mock

import pytest

from tbhbdev.alpha import command, command_modules


def test_command_table() -> None:
    names = """
        agent agent-env brief gh-app gh-shim guard-agent-model guard-gh-config-dir
        guard-gh-poll guard-gh-pr guard-op guard-ops-no-lazy-stop guard-scripts
        guard-sendmessage-clock identity install issue merge merge-queue-remove
        monorepo pr pre-push-guard repo review run-stop-hook session-context state-guard
        verify-range version-check worktree-sweep
    """
    table = command.COMMAND_MODULES
    assert command.COMMANDS.keys() == table.keys() == set(names.split())
    modules = {
        import_module(getattr(command_modules, name).__name__)
        for name in command_modules.__all__
    }
    resolved = set()
    for module_name, function_name in table.values():
        module = import_module(module_name)
        assert callable(getattr(module, function_name))
        resolved.add(module)
    assert resolved == modules


def test_dispatch_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=7)
    monkeypatch.setattr(command_modules.guard_gh_pr, "main", run)
    assert command.main(["guard-gh-pr", "--help"], "p") == 7
    run.assert_called_once_with()


def test_dispatch_merge_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        command.main(["merge", "--help"], "p")
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("usage: p merge ")


def test_dispatch_agent_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.main(["agent", "--help"], "p") == 0
    assert capsys.readouterr().out.startswith("usage: p agent ")


def test_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.main(["unknown", "--help"], "p") == 2
    assert capsys.readouterr() == (
        "",
        "p: unknown command 'unknown'\n" + command.USAGE.format(prog="p"),
    )


def test_missing_command_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(command.COMMAND_MODULES, "agent", ("missing_module", "main"))
    assert command.main(["agent", "--help"], "p") == 1
    assert capsys.readouterr() == ("", "p agent: cannot load module 'missing_module'\n")


def test_missing_dependency_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ModuleNotFoundError(name="missing_dependency")
    monkeypatch.setattr(command.importlib, "import_module", Mock(side_effect=error))
    with pytest.raises(ModuleNotFoundError) as exc:
        command.main(["agent", "--help"], "p")
    assert exc.value is error
