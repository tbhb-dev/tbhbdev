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
        ("fix/topic", "ba", 1, "the start ref requires a fast-forward push"),
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
    monkeypatch.setattr(pg, "git", Mock(return_value=(ancestry, "")))
    local, old = shas
    ref = f"refs/heads/{branch}"
    problems = pg.push_problems(
        "origin",
        [f"HEAD {local * 40} {ref} {old * 40}"],
        agent_push=True,
        run_name="example",
    )
    assert problems == ([f"{ref}: {expected}"] if expected else [])
