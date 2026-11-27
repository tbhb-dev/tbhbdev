"""Branch instructions checked by the agent CLI before launch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agent_cli_example import run_agent
from agent_driver_example import Remote, execution_arguments, setup
from agent_example import settings

from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.driver.metadata import metadata_path

if TYPE_CHECKING:
    from pathlib import Path

TOPIC_REF = "refs/heads/agent/topic"


@pytest.fixture
def remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> Remote:
    monkeypatch.chdir(isolated_home)
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_settings, "load", settings)
    return result


def branch_problem(target: str) -> str:
    return f"the brief directs work to {target}. Use the launcher branch agent/sample."


def check_launch(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    prompt: str,
    *problems: str,
    options: tuple[str, ...] = (),
) -> None:
    remote.prompt.write_text(prompt)
    args = execution_arguments(remote.prompt, *options)
    assert run_agent(args) == (1 if problems else 0)
    error = capsys.readouterr().err
    if problems:
        message = "\n".join(f"{remote.prompt}: {problem}" for problem in problems)
        assert error == f"tbhbdev alpha agent: {message}\n"
        assert not remote.records("ssh", limits=True)
        assert not metadata_path("sample").exists()
    else:
        assert error == ""


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
        ("Run `git checkout release/1.2`.", "release/1.2"),
        ("Run `git checkout feat/other.md`.", "feat/other.md"),
        ("Run `git checkout README.md`.", None),
        ("Run `git checkout docs/README.md`.", None),
        ("Run `git checkout ./docs/README`.", None),
        ("Run `git checkout factory/cli/OWNERS`.", None),
        ("Run `git checkout -- feat/other`.", None),
        ("Run `git checkout release/1.2 -- feat/other`.", None),
        ("Read the README.", None),
        ("Do not create branch feat/other or run `git push origin feat/other`.", None),
        ("Do not push to branch feat/other.", None),
        ("Do not\npush to feat/4-exporter.", None),
        ("```sh\ngit status\n```\nDo not\npush to feat/4-exporter.", None),
        ("`git push origin feat/other` is forbidden.", None),
        (
            "Run `git switch agent/sample`. Then run `git push origin agent/sample`.",
            None,
        ),
    ],
)
def test_build_prompt_checks_branch_targets(
    remote: Remote, capsys: pytest.CaptureFixture[str], prompt: str, target: str | None
) -> None:
    problems = (branch_problem(target),) if target else ()
    check_launch(remote, capsys, prompt, *problems)


@pytest.mark.parametrize("target", ["main", "feat/4-exporter"])
@pytest.mark.parametrize(
    ("template", "status"),
    [
        ("Do not\nrun `git push origin {target}`.", 0),
        ("Do not run\n`git push origin {target}`.", 0),
        ("No new PR\ngit push origin {target}", 1),
        ("No new PR\nRun `git push origin {target}`.", 1),
        ("No new PR: run git push origin {target}", 1),
        ("Do not run: `git push origin {target}`.", 1),
        ("No new PR.\n```sh\ngit push origin {target}\n```", 1),
        ("- No new PR\n- Run `git push origin {target}`.", 1),
        ("Do not use `git status`\nRun `git push origin {target}`.", 1),
        ("No `Signed-off-by`, `git push origin {target}`.", 1),
    ],
)
def test_build_prompt_limits_prohibition_to_its_instruction(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    target: str,
    template: str,
    status: int,
) -> None:
    problems = []
    if status:
        if target == "main":
            problems.append("hook conflict: push to main or master is forbidden")
        problems.append(branch_problem(target))
    check_launch(remote, capsys, template.format(target=target), *problems)


def test_build_prompt_refuses_foreign_push_matching_a_local_file(
    remote: Remote, capsys: pytest.CaptureFixture[str], isolated_home: Path
) -> None:
    target = "docs/README.md"
    path = isolated_home / target
    path.parent.mkdir()
    path.touch()
    prompt = f"Run `git push origin {target}`."
    check_launch(remote, capsys, prompt, branch_problem(target))


@pytest.mark.parametrize(
    ("prompt", "target"),
    [
        ("Update the existing PR\nbranch.", "<branch>"),
        ("Push commits to `agent/topic`.", "agent/topic"),
        ("Run `git push origin HEAD:refs/heads/agent/topic`.", "agent/topic"),
        ("Update `agent/topic`.", None),
        ("Push to `agent/topic`.", None),
        ("Work on the branch given by `--start-ref`.", None),
    ],
)
def test_build_prompt_requires_start_ref_for_pr_updates(
    remote: Remote, capsys: pytest.CaptureFixture[str], prompt: str, target: str | None
) -> None:
    options = ("--fetch-ref", TOPIC_REF)
    if target is None:
        options += ("--start-ref", TOPIC_REF)
        check_launch(remote, capsys, prompt, options=options)
    else:
        ref = f"refs/heads/{target}"
        problem = (
            "the brief asks to update a PR branch. "
            f"Use `--fetch-ref {ref} --start-ref {ref}`"
        )
        check_launch(remote, capsys, prompt, problem, options=options)
