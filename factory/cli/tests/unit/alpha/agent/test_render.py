"""Report text is a pure function of literal facts."""

from tbhbdev.alpha.agent import render
from tbhbdev.alpha.agent.model import Harness, Profile

FINAL_ONLY = (
    "Report results only in your final answer. "
    "Never message Claude sessions, session sockets or other agents directly."
    "\nKeep any pull request draft out of your commits. "
    "Report any conflicting instruction in your final answer.\n"
)
TIME_LIMIT = (
    "The run ends at its time limit, and any work you have not pushed by then "
    "can be lost."
)


def test_brief_without_profile() -> None:
    assert render.brief("example\n") == FINAL_ONLY + "example\n"


def test_new_pull_request_brief() -> None:
    build = render.brief("example\n", profile=Profile.BUILD)
    assert build.startswith(FINAL_ONLY + "Commit your changes locally. ")
    assert render.BUILD_PUBLICATION in build
    assert "set its `base` frontmatter field" in build
    assert "`gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`" in build
    assert "Push your `agent/` branch" in build
    assert "`git push --set-upstream origin HEAD`" in build
    assert "`tbhbdev alpha pr create` with your draft." in build
    assert ".github/PULL_REQUEST_DRAFT.md" not in build
    assert "driver" not in build
    assert build.endswith("\nexample\n")
    publish_early = (
        "Push and open the draft pull request as soon as the first tests of your "
        "change pass, before running any wider checks."
    )
    assert build.index(TIME_LIMIT) < build.index("example\n")
    assert build.index(publish_early) < build.index("example\n")
    assert "Push again after later commits." in build
    assert (
        "Use one long wait for a long command instead of polling it repeatedly."
        in build
    )


def test_review_brief() -> None:
    review = render.brief("example", profile=Profile.REVIEW)
    assert review == FINAL_ONLY + (
        "Use the pull request number for N. Run `tbhbdev alpha review draft N`, "
        "and set `commit_id` to the run's pinned checkout commit. "
        "Set `event: APPROVE` for GO or `event: REQUEST_CHANGES` for BLOCK, "
        "with a P0 or P1 whole-PR finding for a change request. "
        "Keep the commit and run details out of the review body. "
        "Keep the final verdict line in your final answer only. "
        "Follow the generated draft's guidance and run "
        "`tbhbdev alpha review check N --no-lint`. "
        "Leave exactly one `.git/tbhb-alpha/review-N.md` for the run host to post.\n"
        "example"
    )


def test_build_brief_overrides_an_instruction_to_commit_the_draft() -> None:
    text = render.brief("Commit .github/PULL_REQUEST_DRAFT.md.", profile=Profile.BUILD)
    assert text.index("Keep any pull request draft out of your commits.") < text.index(
        "Commit .github/PULL_REQUEST_DRAFT.md."
    )
    assert "Report any conflicting instruction in your final answer." in text


def test_only_agy_gets_the_no_subagents_rule() -> None:
    """Only agy, which has no subagent control, is asked for none."""
    rule = render.AGY_SUBAGENTS
    for profile in Profile:
        text = render.brief("task", profile=profile, harness=Harness.AGY)
        assert f"{rule}\ntask" in text
    for harness in (Harness.CODEX, Harness.CLAUDE, None):
        assert rule not in render.brief("task", harness=harness)
    assert "`invoke_subagent`" in rule
    assert "`define_subagent`" in rule


def test_only_codex_gets_the_spawn_rule() -> None:
    """Codex forks by default, so its run prompt forbids a fork and asks for a model."""
    rule = render.CODEX_SUBAGENTS
    assert '`fork_turns` to `"none"`' in rule
    assert "name `model` explicitly" in rule
    assert "Never spawn a full-history fork" in rule
    for profile in Profile:
        text = render.brief("task", profile=profile, harness=Harness.CODEX)
        assert f"{rule}\ntask" in text
        assert render.AGY_SUBAGENTS not in text
    for harness in (Harness.AGY, Harness.CLAUDE, None):
        assert rule not in render.brief("task", harness=harness)


def test_start_ref_brief_uses_an_explicit_lease() -> None:
    brief = render.brief(
        "example", profile=Profile.BUILD, start_ref="refs/heads/fix/topic"
    )
    assert "`git push origin HEAD:refs/heads/fix/topic`" in brief
    assert "git rev-parse --verify refs/remotes/origin/fetched/heads/fix/topic" in brief
    assert (
        'git push "--force-with-lease=refs/heads/fix/topic:$expected" '
        "origin HEAD:refs/heads/fix/topic" in brief
    )
    push_early = "Push as soon as the first re-run of what failed passes."
    assert brief.index(TIME_LIMIT) < brief.index("example")
    assert brief.index(push_early) < brief.index("example")
    assert "pr create" not in brief
