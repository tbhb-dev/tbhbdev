"""Describe the state of a build at its fault exit."""

from tbhbdev.alpha.agent.run_host import draft_pr

SAVE = "rescue: save failed run work"


def test_title() -> None:
    assert draft_pr.title([SAVE, "first", "second"]) == "first"
    assert draft_pr.title([SAVE]) == SAVE
    assert draft_pr.title(["a" * 101]) == "a" * 100


def test_body() -> None:
    saved = draft_pr.body(124, ["gazelle", "lint"], saved=True).lower()
    unfinished = "the last commit is an automatic save of unfinished work"
    assert saved.index("hit the time limit") < saved.index("gazelle, lint")
    assert saved.index("gazelle, lint") < saved.index(unfinished)
    assert saved.index(unfinished) < saved.index("has not passed review")
    other = draft_pr.body(11, [], saved=False).lower()
    assert "ended with code 11" in other
    assert "no record of a pre-push check" in other
    assert unfinished not in other
