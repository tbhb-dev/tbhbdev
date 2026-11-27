"""Pull request creation checks the branch before reading the draft."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev import userconfig
from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "case",
    [
        ((0, "agent/prepare-EXAMPLEHOST01"), "", "branch name contains a host name"),
        ((0, ""), "", "cannot read the current branch"),
        ((128, ""), "", "cannot read the current branch"),
        (
            (0, "agent/topic"),
            '[agent]\nhost = "invalid"\n',
            "cannot read known host names",
        ),
    ],
)
def test_create_checks_the_branch_for_host_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[tuple[int, str], str, str],
) -> None:
    git_result, config_text, message = case
    monkeypatch.setattr(socket, "gethostname", lambda: "examplehost01.example.test")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = userconfig.path()
    config.parent.mkdir()
    config.write_text(config_text)
    monkeypatch.setattr(pr, "require_git_hooks", Mock(return_value=True))
    monkeypatch.setattr(pr, "find_tools", Mock(return_value={}))
    git = Mock(return_value=git_result)
    monkeypatch.setattr(pr, "git", git)
    read_checked = Mock()
    monkeypatch.setattr(pr, "read_checked", read_checked)

    assert pr.main(["create"], env={}) == 1
    assert capsys.readouterr().err == f"tbhbdev alpha pr: {message}\n"
    git.assert_called_once_with("branch", "--show-current")
    read_checked.assert_not_called()
