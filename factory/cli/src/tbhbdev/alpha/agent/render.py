"""The brief a harness receives, from literal text."""

from tbhbdev.alpha.agent.model import Harness, Profile

FINAL_ONLY = (
    "Report results only in your final answer. "
    "Never message Claude sessions, session sockets or other agents directly.\n"
    "Keep any pull request draft out of your commits. "
    "Report any conflicting instruction in your final answer."
)
# agy has no setting, permission rule or per-run hook for its subagents, and a
# subagent defaults to the run's model, so this rule is a request, not enforcement.
AGY_SUBAGENTS = (
    "Do not launch subagents: never call `invoke_subagent` or `define_subagent`. "
    "Do the work yourself."
)
# Codex 0.171.0 has no setting, flag or feature switch that turns off a
# full-history fork (`fork_turns` omitted or "all"). Its source applies a named or
# configured default model to a fork too, so this rule keeps the history out of a
# child and names the model; it isn't a model bypass guard. It is a request, not
# enforcement, and no run has confirmed the source reading.
CODEX_SUBAGENTS = (
    'Every `spawn_agent` call must set `fork_turns` to `"none"` and name '
    "`model` explicitly. Never spawn a full-history fork, which is a spawn "
    'that omits `fork_turns` or sets it to `"all"`.'
)
BUILD_TIME_LIMIT = (
    "The run ends at its time limit, and any work you have not pushed by then "
    "can be lost."
)
BUILD_PUBLICATION = (
    "Push your `agent/` branch with `git push --set-upstream origin HEAD`. "
    "If this run should open a pull request, write a draft at the repository's "
    "default draft path and set its `base` frontmatter field. To find the "
    "repository's default branch, run "
    "`gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`. "
    "Then run `tbhbdev alpha pr create` with your draft."
)
REVIEW_PUBLICATION = (
    "Use the pull request number for N. Run `tbhbdev alpha review draft N`, "
    "and set `commit_id` to the run's pinned checkout commit. "
    "Set `event: APPROVE` for GO or `event: REQUEST_CHANGES` for BLOCK, "
    "with a P0 or P1 whole-PR finding for a change request. "
    "Keep the commit and run details out of the review body. "
    "Keep the final verdict line in your final answer only. "
    "Follow the generated draft's guidance and run "
    "`tbhbdev alpha review check N --no-lint`. "
    "Leave exactly one `.git/tbhb-alpha/review-N.md` for the run host to post."
)


def brief(
    text: str,
    *,
    profile: Profile | None = None,
    harness: Harness | None = None,
    start_ref: str | None = None,
) -> str:
    if harness is Harness.AGY:
        text = f"{AGY_SUBAGENTS}\n{text}"
    elif harness is Harness.CODEX:
        text = f"{CODEX_SUBAGENTS}\n{text}"
    if profile is Profile.BUILD and start_ref is not None:
        branch = start_ref.removeprefix("refs/heads/")
        text = (
            "Commit your changes locally and update the existing pull request branch.\n"
            f"{BUILD_TIME_LIMIT} "
            "Push as soon as the first re-run of what failed passes.\n"
            f"For a fast-forward, run `git push origin HEAD:{start_ref}`.\n"
            "To publish a rewrite, use a lease with the fetched commit.\n"
            "```sh\n"
            "expected=$(git rev-parse --verify "
            f"refs/remotes/origin/fetched/heads/{branch}) &&\n"
            f'  git push "--force-with-lease={start_ref}:$expected" origin '
            f"HEAD:{start_ref}\n"
            f"```\n{text}"
        )
    elif profile is Profile.BUILD:
        text = (
            f"Commit your changes locally. {BUILD_PUBLICATION}\n"
            f"{BUILD_TIME_LIMIT} "
            "Push and open the draft pull request as soon as the first tests of your "
            "change pass, before running any wider checks. "
            "Push again after later commits. "
            "Use one long wait for a long command instead of polling it repeatedly.\n"
            f"{text}"
        )
    elif profile is Profile.REVIEW:
        text = f"{REVIEW_PUBLICATION}\n{text}"
    return f"{FINAL_ONLY}\n{text}"
