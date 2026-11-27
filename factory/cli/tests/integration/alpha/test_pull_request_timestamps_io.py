# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Retain the oldest trailer timestamp when creating or editing a pull request."""

import json
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from fake_tool import PR_ORIGIN

from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@pytest.mark.usefixtures("isolated_home", "pr_checkout")
@pytest.mark.parametrize("verb", ["create", "edit"])
def test_pr_keeps_earliest_start(verb: str, monkeypatch: pytest.MonkeyPatch) -> None:
    early = "LLM harness=codex;model=openai:m;started_at=2026-11-14T00:02:41Z"
    late = early.replace("00:02:41", "00:09:00")
    messages = [f"one\n\nGenerated-by: {late}", f"two\n\nAssisted-by: {early}"]
    if verb == "create":
        for message in messages:
            assert git("commit", "--allow-empty", "-qm", message)[0] == 0

    remote = PR_ORIGIN.removeprefix("git@").removesuffix(".git").replace(":", "/")
    pages = [[{"commit": {"message": message}}] for message in messages]
    actual_run = pr.run
    posted = Mock()

    def run(
        argv: Sequence[str], *, cwd: Path | None = None, stdin: str = ""
    ) -> CompletedProcess[str]:
        if argv[0] != "gh":
            return actual_run(argv, cwd=cwd, stdin=stdin)
        match list(argv[1:3]):
            case ["pr", "view"] if verb == "create":
                return CompletedProcess(argv, 1, "", "No Pull Requests Found")
            case ["pr", "view"]:
                return CompletedProcess(
                    argv, 0, json.dumps({"url": f"https://{remote}/pull/40"}), ""
                )
            case ["api", _]:
                return CompletedProcess(argv, 0, json.dumps(pages), "")
            case ["pr", action]:
                posted(action, stdin)
            case _:
                pass
        return CompletedProcess(argv, 0, "{}", "")

    draft = pr.Checked([], "fix: x", ["# fix: x", "", "Example."], {"base": "main"})
    monkeypatch.setattr(pr, "read_checked", Mock(return_value=draft))
    monkeypatch.setattr(pr, "check_size", Mock())
    monkeypatch.setattr(pr, "post_text", Mock())
    monkeypatch.setattr(pr, "find_tools", Mock(return_value={}))
    monkeypatch.setattr(pr, "find_tool", str)
    monkeypatch.setattr(pr, "run", run)
    args = ["create"] if verb == "create" else ["edit", str(pr.DRAFT), "--pr", "40"]
    assert pr.main(args, env={}) == 0
    posted.assert_called_once_with(verb, f"Example.\n\nGenerated-by: {early}\n")
