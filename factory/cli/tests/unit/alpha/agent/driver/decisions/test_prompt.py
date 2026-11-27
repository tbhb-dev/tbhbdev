"""Test coordinator prompt decisions with literal values."""

from __future__ import annotations

import pytest

from tbhbdev.alpha.agent.driver.decisions import prompt
from tbhbdev.alpha.agent.model import Profile


@pytest.mark.parametrize(
    ("text", "profile"),
    [
        ("The earlier PR was on `agent/topic`.", Profile.BUILD),
        ("Do not update the PR branch or use agent/topic.", Profile.BUILD),
        ("Update the PR branch.", Profile.REVIEW),
    ],
)
def test_prompt_without_a_build_update_or_explicit_ref_problem(
    text: str, profile: Profile
) -> None:
    branch = "agent/sample" if profile is Profile.BUILD else None
    assert not prompt.prompt_launch_problems(text, [], None, build_branch=branch)


MERGE_CONFLICT = (
    "hook conflict: merge commits lack the required Generated-by trailer. "
    "Use git merge --no-commit, then a plain git commit"
)
OMISSION_CONFLICT = "hook conflict: let the hooks stamp the identity trailers"
MANUAL_CONFLICT = (
    "hook conflict: leave hand-written identity trailers out of the commit message"
)
BYPASS_CONFLICT = "hook conflict: hook bypass is forbidden"
PUSH_CONFLICT = "hook conflict: push to main or master is forbidden"
INSTALL_CONFLICT = "hook conflict: hook install is forbidden"
BRANCH_WARNING = "use the launcher branch agent/sample-run instead of "


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            (
                "1. Run `git fetch origin main` then "
                '`git merge --no-ff -m "chore: merge main into the branch" '
                "origin/main`."
            ),
            MERGE_CONFLICT,
        ),
        (
            "Run `git fetch origin main` then `git merge --no-ff origin/main`.",
            MERGE_CONFLICT,
        ),
        ("git merge origin/main; git merge --no-commit origin/main", MERGE_CONFLICT),
        ("git merge origin/main\nUse --no-commit if needed.", MERGE_CONFLICT),
        (
            (
                "Commit with plain git commit; no Signed-off-by, "
                "no Generated-by or Co-authored-by trailers."
            ),
            OMISSION_CONFLICT,
        ),
        ("Commit without an Assisted-by trailer.", OMISSION_CONFLICT),
        ("Omit the Generated-by line.", OMISSION_CONFLICT),
        ("Strip Generated-by.", OMISSION_CONFLICT),
        ('git commit -m "feat: x" -m "Generated-by: LLM model=sol"', MANUAL_CONFLICT),
        ('git commit --message="Assisted-by: example"', MANUAL_CONFLICT),
        ("git commit -F 'Generated-by: example'", MANUAL_CONFLICT),
        ("git commit --file 'Assisted-by: example'", MANUAL_CONFLICT),
        ('git commit --no-verify -m "x"', BYPASS_CONFLICT),
        ("git commit -n", BYPASS_CONFLICT),
        ("git commit -n\ngit status", BYPASS_CONFLICT),
        ("git push --no-verify", BYPASS_CONFLICT),
        ("Avoid skipping hooks. Use --no-verify.", BYPASS_CONFLICT),
        ("git push origin main", PUSH_CONFLICT),
        ("git push origin main\ngit status", PUSH_CONFLICT),
        ("git push origin HEAD:main", PUSH_CONFLICT),
        ("git push master", PUSH_CONFLICT),
        ("git push origin HEAD:master", PUSH_CONFLICT),
        ("git push origin refs/heads/main", PUSH_CONFLICT),
        ("git push origin refs/heads/master", PUSH_CONFLICT),
        ("Run prek install first.", INSTALL_CONFLICT),
        ("Run pre-commit install first.", INSTALL_CONFLICT),
    ],
)
def test_check_hooks_refuses_conflict(text: str, problem: str) -> None:
    assert prompt.check_hooks(text) == [problem]


@pytest.mark.parametrize(
    ("text", "branch"),
    [
        (
            (
                "Create branch feat/graph-baseline from origin/main "
                "and commit the work there."
            ),
            "feat/graph-baseline",
        ),
        ("git switch -c feat/graph-baseline", "feat/graph-baseline"),
        ("git checkout -b fix/other origin/main", "fix/other"),
        ("git checkout -B fix/other", "fix/other"),
        ("git checkout fix/other", "fix/other"),
        ("git switch -C fix/other", "fix/other"),
        ("git switch fix/other", "fix/other"),
        ("git branch fix/other", "fix/other"),
        ("git worktree add .worktrees/other -b fix/other", "fix/other"),
        ("Commit on branch `fix/other`.", "fix/other"),
        ("Work on branch fix/other.", "fix/other"),
        ("git branch agent/other", "agent/other"),
        ("git switch -c agent/Sample-run", "agent/Sample-run"),
    ],
)
def test_foreign_branch_warns_without_a_hook_conflict(text: str, branch: str) -> None:
    assert prompt.check_hooks(text) == []
    assert prompt.branch_warnings(text, run="sample-run") == [BRANCH_WARNING + branch]


@pytest.mark.parametrize(
    "text",
    [
        "Never use --no-verify.",
        (
            "No hook skips (no SKIP=, PREK_SKIP=, --no-verify, hooksPath override); "
            "a hook failure means stop and report."
        ),
        (
            "Never run `prek install`, `pre-commit install`, `tbhbdev alpha install`, "
            "or any other hook installer."
        ),
        "(never bare `prek install`)",
        "Never push to main. Push with `git push origin HEAD:docs/some-branch`.",
        (
            "git merge --no-ff --no-commit origin/main then finish with a plain "
            '`git commit -m "chore(o11y): merge main"` (no -F, no --no-verify).'
        ),
        "git merge-base --is-ancestor 969da64 HEAD",
        "git merge --ff-only origin/main",
        "git merge --squash origin/main",
        "git merge --abort",
        "git merge --abort\ngit status",
        "git merge --continue",
        "git merge --quit",
        "Avoid `git merge --no-ff origin/main`.",
        (
            "No Signed-off-by, no Co-authored-by. Author and committer must both read "
            "the agent identity; the hooks stamp the Generated-by trailer."
        ),
        (
            "Commit once (Conventional Commits, no Signed-off-by or Co-authored-by; "
            "the Generated-by trailer must name harness and model)."
        ),
        "Never remove the Generated-by trailer.",
        'Do not use `git commit -m "Generated-by: example"`.',
        'git commit -m "feat: x"',
        "git commit -F msg.txt",
        "Do not push to main; the driver pushes your agent/sample-run branch.",
        "Do not run `git push origin HEAD:main`.",
        "Never create or switch branches.",
        "Do not create branch fix/other.",
        "Don't run `git switch -c fix/other`.",
        "git switch -c agent/sample-run",
        "git worktree add .worktrees/sample-run -b agent/sample-run origin/main",
        "git checkout origin/main -- README.md",
        "git branch --list",
        "This fixes the main branch build.",
        "git push origin HEAD:docs/main",
        "git push origin mainland",
        "git push origin topic; echo main",
        "git push origin topic\nmain is the default branch.",
    ],
)
def test_check_hooks_accepts_clean_text(text: str) -> None:
    assert prompt.check_hooks(text) == []
    assert prompt.branch_warnings(text, run="sample-run") == []
