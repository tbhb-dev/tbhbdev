"""Check publication instructions for a start-ref build."""

from tbhbdev.alpha.agent import render
from tbhbdev.alpha.agent.model import Profile, RunKind


def test_start_ref_brief_uses_an_explicit_lease() -> None:
    brief = render.brief(
        RunKind.EXEC, "example", profile=Profile.BUILD, start_ref="refs/heads/fix/topic"
    )
    assert "`git push origin HEAD:refs/heads/fix/topic`" in brief
    assert "git rev-parse --verify refs/remotes/origin/fetched/heads/fix/topic" in brief
    assert (
        'git push "--force-with-lease=refs/heads/fix/topic:$expected" '
        "origin HEAD:refs/heads/fix/topic" in brief
    )
    assert "pr create" not in brief
