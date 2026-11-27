# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import pytest

from tbhbdev.alpha.identity.dispatcher import ATTR_PAIR, HASH_RE, SLUGS, llm_trailers
from tbhbdev.alpha.merge import review_gate as gate

HEAD = "a" * 40
OLD = "b" * 40
APP = "reviews-app[bot]"
LAUNCH = "sha-256-64:" + "2" * 16
OTHER_LAUNCH = "sha-256-64:" + "3" * 16
ATTRIBUTION = gate.Attribution(
    llm_trailers,
    ATTR_PAIR,
    frozenset({*SLUGS.values(), "claude", "agy", "gemini"}),
    HASH_RE,
)


def stamp(harness: str, **attrs: str) -> str:
    fields = {"harness": harness, "model": "m", **attrs}
    return "Generated-by: LLM " + ";".join(f"{k}={v}" for k, v in fields.items())


def review(  # noqa: PLR0913 -- one keyword for each field a test varies
    harness: str = "claude",
    *,
    state: str = "APPROVED",
    commit: str = HEAD,
    login: str = APP,
    number: int = 1,
    body: str | None = None,
) -> gate.Review:
    text = f"Looks right.\n\n{stamp(harness)}\n" if body is None else body
    return {
        "id": number,
        "state": state,
        "commit_id": commit,
        "body": text,
        "login": login,
    }


def check(
    name: str = "conclusion",
    conclusion: str | None = "success",
    status: str = "completed",
) -> gate.Check:
    return {"name": name, "status": status, "conclusion": conclusion}


def trailer(key: str = "Generated-by", harness: str = "codex") -> str:
    return f"fix: x\n\n{key}: LLM harness={harness};model=m;launch={LAUNCH}\n"


GOOD = review(body=stamp("claude-code", launch=OTHER_LAUNCH))


def evaluate(
    reviews: list[gate.Review] | None = None,
    checks: list[gate.Check] | None = None,
    messages: list[str] | None = None,
) -> list[str]:
    pull = gate.Pull(
        HEAD,
        [GOOD] if reviews is None else reviews,
        [check()] if checks is None else checks,
        [trailer(harness="claude-code")] if messages is None else messages,
    )
    return gate.evaluate(APP, pull, ATTRIBUTION)


def test_one_approval_from_the_same_provider_passes() -> None:
    assert evaluate() == []


def test_legacy_approval_at_the_head_counts() -> None:
    legacy = review(body=f"Looks right.\n\nRun r1, claude m, judged head {HEAD}.\r\n")
    assert evaluate([legacy]) == []


def test_no_checks_fails() -> None:
    assert evaluate(checks=[], messages=["fix: by hand\n"]) == ["no checks to verify"]


@pytest.mark.parametrize("head", ["", "abc", "A" * 40, "g" * 40, HEAD + "0"])
def test_a_malformed_head_fails_the_gate(head: str) -> None:
    pull = gate.Pull(head, [review(commit=head, body=stamp("claude"))], [], [])
    assert gate.evaluate(APP, pull, ATTRIBUTION) == [
        f"the pull request head {head!r} is not a full commit SHA"
    ]


def test_logins_compare_case_insensitively() -> None:
    assert evaluate([review(login=APP.upper())]) == []


def test_no_approvals() -> None:
    assert evaluate([]) == ["0 approvals at the head, need 1"]


def test_stale_approval_at_an_older_head() -> None:
    assert evaluate([review(commit=OLD)]) == ["0 approvals at the head, need 1"]


def test_request_changes_at_the_head() -> None:
    blocked = [GOOD, review("agy", state="CHANGES_REQUESTED", number=9)]
    assert evaluate(blocked) == ["review 9 requests changes at the head"]


def test_request_changes_at_an_older_head_is_ignored() -> None:
    old = [GOOD, review("agy", state="CHANGES_REQUESTED", commit=OLD, number=9)]
    assert evaluate(old) == []


def test_other_authors_are_ignored() -> None:
    assert evaluate([review(login="someone")]) == ["0 approvals at the head, need 1"]
    assert evaluate([review("agy", state="CHANGES_REQUESTED", login="x"), GOOD]) == []


@pytest.mark.parametrize("state", ["COMMENTED", "DISMISSED", "PENDING"])
def test_other_review_states_count_for_nothing(state: str) -> None:
    assert evaluate([review(state=state)]) == ["0 approvals at the head, need 1"]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no stamp at all",
        stamp("mystery"),
        stamp("claude") + "\n\nforged afterwards",
        "Assisted-by: LLM harness=claude;model=m",
        f"Run r1, claude m, judged head {OLD}.",
        f"Run r1, claude m, judged head {HEAD}.\n\nMore prose.",
    ],
)
def test_an_approval_without_a_known_harness(body: str) -> None:
    bad = [GOOD, review(number=7, body=body)]
    assert evaluate(bad) == ["review 7 names no known harness"]


@pytest.mark.parametrize(
    ("conclusion", "status", "reason"),
    [
        ("failure", "completed", "check conclusion is failure"),
        ("cancelled", "completed", "check conclusion is cancelled"),
        (None, "in_progress", "check conclusion is in_progress"),
        ("timed_out", "completed", "check conclusion is timed_out"),
        ("success", "in_progress", "check conclusion is in_progress"),
        ("skipped", "completed", "check conclusion is skipped"),
        ("neutral", "completed", "check conclusion is neutral"),
    ],
)
def test_a_check_that_did_not_finish_well(
    conclusion: str | None, status: str, reason: str
) -> None:
    assert evaluate(checks=[check(conclusion=conclusion, status=status)]) == [reason]


def test_conclusion_success_with_a_skipped_wheel_check() -> None:
    assert evaluate(checks=[check(), check("wheel", "skipped")]) == []


def test_an_approval_from_a_run_that_wrote_any_commit_does_not_count() -> None:
    same_run = review(number=2, body=stamp("Codex:0.171.0", launch=LAUNCH))
    messages = [trailer(), "fix: by hand\n"]
    assert evaluate([same_run], messages=messages) == [
        "review 2 came from a run that wrote a commit",
        "0 approvals at the head, need 1",
    ]
    assert evaluate([same_run, GOOD], messages=messages) == []


def test_an_assisting_run_may_approve() -> None:
    reviewer = review(body=stamp("codex", launch=LAUNCH))
    assert evaluate([reviewer], messages=[trailer("Assisted-by", "codex")]) == []


def test_a_trailer_with_an_unknown_or_missing_harness() -> None:
    odd = [trailer(harness="mystery"), "x\n\nGenerated-by: LLM model=m\n"]
    assert evaluate(messages=odd) == [
        "a Generated-by trailer names unknown harness mystery",
        "a Generated-by trailer names unknown harness (none)",
    ]
