"""Check review launch identity requirements."""

from __future__ import annotations

import pytest

from tbhbdev.alpha.agent.driver.decisions import launcher as rules
from tbhbdev.alpha.agent.model import Invalid, Profile


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("✳ tools", "tools"),
        ("tools", "tools"),
        ("  ⠂ helper-session  ", "helper-session"),
        ("✳", None),
        ("", None),
    ],
)
def test_session_name_drops_the_status_glyph(title: str, name: str | None) -> None:
    assert rules.session_name(title) == name


@pytest.mark.parametrize(
    ("role", "profile", "expected"),
    [
        ("reviewer", Profile.BUILD, True),
        (None, Profile.REVIEW, True),
        ("builder", Profile.BUILD, False),
        (None, Profile.BUILD, False),
    ],
)
def test_a_review_profile_run_is_reviewer_role_too(
    role: str | None, profile: Profile, *, expected: bool
) -> None:
    assert rules.is_reviewer(role, profile) is expected


@pytest.mark.parametrize("launcher_name", ["tools", "helper", "alpha"])
def test_any_named_session_may_launch_reviews(launcher_name: str) -> None:
    rules.check(launcher_name)


def test_a_reviewer_run_without_a_session_name_is_refused() -> None:
    with pytest.raises(Invalid) as caught:
        rules.check(None)
    assert str(caught.value) == (
        "a reviewer-role run needs the launching session's name, from the "
        "tmux pane title; TMUX_PANE or its title is missing, so "
        "name the launching session via the tmux pane title"
    )
