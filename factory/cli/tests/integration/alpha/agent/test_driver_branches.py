"""Branch instructions checked by the agent CLI before launch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agent_cli_example import run_agent
from agent_driver_example import (
    BODY,
    Remote,
    execution_arguments,
    setup,
    write_standalone_fake,
)
from agent_example import settings

from tbhbdev.alpha.agent import driver
from tbhbdev.alpha.agent import settings as agent_settings

if TYPE_CHECKING:
    from pathlib import Path

COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> Remote:
    monkeypatch.chdir(isolated_home)
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_settings, "load", settings)
    return result


def launch(remote: Remote, *options: str) -> int:
    return run_agent(execution_arguments(remote.prompt, *options))


@pytest.mark.parametrize(
    ("prompt", "options"),
    [
        ("The earlier PR was on `agent/other`.", ()),
        ("Create branch `agent/other`.", ("--profile", "review", "--ref", COMMIT)),
    ],
)
def test_prompt_with_foreign_branch_warns_and_launches(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    prompt: str,
    options: tuple[str, ...],
) -> None:
    write_standalone_fake(remote.bin / "tmux", BODY.replace("sample", "sample-run"))
    remote.prompt.write_text(prompt)
    args = execution_arguments(remote.prompt, *options)
    args[-2] = "sample-run"
    assert run_agent(args) == 0
    assert capsys.readouterr().err == (
        f"warning: {remote.prompt}: use the launcher branch agent/sample-run "
        "instead of agent/other\n"
    )
    assert driver.metadata_path("sample-run").is_file()


@pytest.mark.parametrize(
    ("prompt", "target"),
    [
        ("Create branch `feat/4-exporter`.", "feat/4-exporter"),
        ("Run `git checkout -b feat/4-exporter`.", "feat/4-exporter"),
        ("Run `git switch feat/4-exporter`.", "feat/4-exporter"),
        ("Push to `feat/4-exporter`.", "feat/4-exporter"),
        ("Push feat/4-exporter.", "feat/4-exporter"),
        (
            "Run `git push --set-upstream origin HEAD:feat/4-exporter`.",
            "feat/4-exporter",
        ),
        ("Do not open a PR, but push feat/4-exporter.", "feat/4-exporter"),
        ("No new PR: push feat/4-exporter.", "feat/4-exporter"),
        ("Do not open a PR.\n\nPush feat/4-exporter.", "feat/4-exporter"),
        ("```sh\necho no\ngit push origin feat/4-exporter\n```", "feat/4-exporter"),
        ("Run `git push origin develop`.", "develop"),
        ("Run `git checkout refs/heads/feat/other.md`.", "refs/heads/feat/other.md"),
        ("Run `git checkout -b feat/other.md`.", "feat/other.md"),
        ("Run `git switch docs/README.md`.", "docs/README.md"),
    ],
)
def test_build_prompt_with_foreign_branch_refuses_launch(
    remote: Remote, capsys: pytest.CaptureFixture[str], prompt: str, target: str
) -> None:
    remote.prompt.write_text(prompt)
    assert launch(remote) == 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha agent: {remote.prompt}: the brief directs work to {target}. "
        "Use the launcher branch agent/sample.\n"
    )
    assert remote.records("ssh", limits=True) == []
    assert not driver.metadata_path("sample").exists()


@pytest.mark.parametrize("target", ["main", "feat/4-exporter"])
@pytest.mark.parametrize(
    ("template", "status"),
    [
        ("Do not\nrun `git push origin {target}`.", 0),
        ("No new PR\ngit push origin {target}", 1),
        ("No new PR: run git push origin {target}", 1),
        ("Do not run: `git push origin {target}`.", 1),
        ("No new PR.\n```sh\ngit push origin {target}\n```", 1),
        ("- No new PR\n- Run `git push origin {target}`.", 1),
        ("Do not use `git status`\nRun `git push origin {target}`.", 1),
    ],
)
def test_build_prompt_limits_prohibition_to_its_instruction(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    target: str,
    template: str,
    status: int,
) -> None:
    remote.prompt.write_text(template.format(target=target))
    assert launch(remote) == status
    error = capsys.readouterr().err
    if status == 0:
        assert error == ""
    else:
        expected = (
            "hook conflict: push to main or master is forbidden"
            if target == "main"
            else f"the brief directs work to {target}"
        )
        assert expected in error
        assert remote.records("ssh", limits=True) == []


@pytest.mark.parametrize(
    "prompt",
    [
        "Read the README.",
        "Do not create branch feat/other or run `git push origin feat/other`.",
        "Do not push to branch feat/other.",
        "Do not\npush to feat/4-exporter.",
        "```sh\ngit status\n```\nDo not\npush to feat/4-exporter.",
        "`git push origin feat/other` is forbidden.",
        "Run `git switch agent/sample`. Then run `git push origin agent/sample`.",
    ],
)
def test_build_prompt_without_foreign_branch_instructions_launches(
    remote: Remote, capsys: pytest.CaptureFixture[str], prompt: str
) -> None:
    remote.prompt.write_text(prompt)
    assert launch(remote) == 0
    assert capsys.readouterr().err == ""


def test_build_prompt_refuses_foreign_push_matching_a_local_file(
    remote: Remote, isolated_home: Path
) -> None:
    (isolated_home / "docs").mkdir()
    (isolated_home / "docs/README.md").touch()
    remote.prompt.write_text("Run `git push origin docs/README.md`.")
    assert launch(remote) == 1
    assert remote.records("ssh", limits=True) == []


@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("README.md", 0),
        ("docs/README.md", 0),
        ("./docs/README", 0),
        ("factory/cli/OWNERS", 0),
        ("-- feat/other", 0),
        ("release/1.2 -- feat/other", 0),
        ("release/1.2", 1),
        ("feat/other.md", 1),
    ],
)
def test_build_prompt_distinguishes_checkout_paths_from_branches(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    target: str,
    status: int,
) -> None:
    remote.prompt.write_text(f"Run `git checkout {target}`.")
    assert launch(remote) == status
    error = capsys.readouterr().err
    if status == 0:
        assert error == ""
    else:
        assert f"the brief directs work to {target}" in error
        assert remote.records("ssh", limits=True) == []


@pytest.mark.parametrize(
    ("prompt", "target"),
    [
        ("Update the existing PR\nbranch.", "<branch>"),
        ("Push commits to `agent/topic`.", "agent/topic"),
        ("Run `git push origin HEAD:refs/heads/agent/topic`.", "agent/topic"),
    ],
)
def test_build_prompt_that_updates_a_pr_branch_needs_start_ref(
    remote: Remote, capsys: pytest.CaptureFixture[str], prompt: str, target: str
) -> None:
    remote.prompt.write_text(prompt)
    assert launch(remote, "--fetch-ref", "refs/heads/agent/topic") == 1
    ref = f"refs/heads/{target}"
    assert capsys.readouterr().err == (
        f"tbhbdev alpha agent: {remote.prompt}: the brief asks to update a PR branch. "
        f"Use `--fetch-ref {ref} --start-ref {ref}`\n"
    )
    assert remote.records("ssh") == []
    assert not driver.metadata_path("sample").exists()


@pytest.mark.parametrize(
    "prompt",
    [
        "Update `agent/topic`.",
        "Push to `agent/topic`.",
        "Work on the branch given by `--start-ref`.",
    ],
)
def test_build_prompt_that_updates_a_pr_branch_with_start_ref_launches(
    remote: Remote, capsys: pytest.CaptureFixture[str], prompt: str
) -> None:
    remote.prompt.write_text(prompt)
    ref = "refs/heads/agent/topic"
    assert launch(remote, "--fetch-ref", ref, "--start-ref", ref) == 0
    assert capsys.readouterr().err == ""
