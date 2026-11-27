"""Check updates to a build's starting branch."""

from unittest.mock import Mock

import pytest

from tbhbdev.alpha import pre_push_guard as pg


@pytest.mark.parametrize(
    "case",
    [
        ("fix/topic", "ba", 0, ""),
        ("agent/example", "ba", 0, "this run may only push refs/heads/fix/topic"),
        (
            "fix/topic",
            "0a",
            0,
            "deletion refused; this run may only update its start ref",
        ),
        ("fix/topic", "b0", 0, "the start ref must already exist; push refused"),
        ("fix/topic", "ba", 1, ""),
        (
            "fix/topic",
            "ba",
            128,
            "git merge-base could not check ancestry; push refused",
        ),
        # The advertised tip c is an ancestor of the fetched start a and the fix b.
        ("fix/topic", "bc", 0, "remote tip changed since fetch; push refused"),
    ],
)
def test_start_ref_push(
    monkeypatch: pytest.MonkeyPatch, case: tuple[str, str, int, str]
) -> None:
    branch, shas, ancestry, expected = case
    monkeypatch.setenv(pg.RUN_START_REF_ENV, "refs/heads/fix/topic")
    monkeypatch.setenv(pg.RUN_START_TIP_ENV, "a" * 40)
    for name in ("remote_tips", "known_objects", "agent_commit_problems"):
        monkeypatch.setattr(pg, name, Mock(return_value=[]))
    git = Mock(side_effect=[(ancestry, ""), (0, "a" * 40)])
    monkeypatch.setattr(pg, "git", git)
    local, old = shas
    ref = f"refs/heads/{branch}"
    problems = pg.push_problems(
        "origin",
        [f"HEAD {local * 40} {ref} {old * 40}"],
        agent_push=True,
        run_name="example",
    )
    assert problems == ([f"{ref}: {expected}"] if expected else [])
    if ancestry:
        git.assert_called_with(
            *pg.SAFE,
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/remotes/origin/fetched/heads/fix/topic^{commit}",
        )


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/master"])
def test_protected_branch_cannot_be_updated_as_start_ref(ref: str) -> None:
    assert "push to a protected branch refused" in pg.destination_problem(
        ref, deleting=False, agent_push=True, run_name="example", start_ref=ref
    )
