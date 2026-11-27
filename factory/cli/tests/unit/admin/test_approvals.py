"""The operator's approval covers only changes named in the decision record."""

import pytest

from tbhbdev.admin.approvals import Approval, approved_decisions

REPOSITORY = "tbhb-dev/tbhbdev"
RULESET_ID = 28092610
PATH = f"/repos/{REPOSITORY}/rulesets/{RULESET_ID}"
THREADS = "required_review_thread_resolution"
DECISION = "2026-11-27-merge-does-not-require-resolved-threads"
APPROVAL = Approval(
    REPOSITORY, RULESET_ID, "pull_request", THREADS, value=False, decision=DECISION
)


def ruleset(value: object, **parameters: object) -> dict[str, object]:
    return {
        "name": "main",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "pull_request", "parameters": {THREADS: value, **parameters}},
        ],
    }


CURRENT = {
    **ruleset(value=True),
    "id": RULESET_ID,
    "source": REPOSITORY,
    "source_type": "Repository",
    "current_user_can_bypass": "never",
}


def test_approved_change() -> None:
    assert approved_decisions(PATH, CURRENT, ruleset(value=False), [APPROVAL]) == (
        DECISION,
    )


@pytest.mark.parametrize(
    ("path", "source", "ruleset_id"),
    [
        (PATH.replace(str(RULESET_ID), "7"), REPOSITORY, 7),
        (PATH.replace(REPOSITORY, "tbhb-dev/other"), "tbhb-dev/other", RULESET_ID),
        (PATH, REPOSITORY, 7),
        (PATH, "tbhb-dev/other", RULESET_ID),
    ],
)
def test_scope(path: str, source: str, ruleset_id: int) -> None:
    current = {**CURRENT, "id": ruleset_id, "source": source}
    assert approved_decisions(path, current, ruleset(value=False), [APPROVAL]) == ()


@pytest.mark.parametrize("value", [True, 0, "false", None])
def test_other_values(value: object) -> None:
    assert approved_decisions(PATH, CURRENT, ruleset(value), [APPROVAL]) == ()


@pytest.mark.parametrize(
    "body",
    [
        ruleset(value=False, require_last_push_approval=False),
        {
            **ruleset(value=False),
            "rules": [
                {"type": "pull_request", "parameters": {THREADS: False}},
            ],
        },
        {
            **ruleset(value=False),
            "bypass_actors": [{"actor_id": 5, "actor_type": "Team"}],
        },
        {
            **ruleset(value=False),
            "conditions": {"ref_name": {"include": ["refs/heads/dev"]}},
        },
        {**ruleset(value=False), "enforcement": "disabled"},
        {**ruleset(value=False), "name": "renamed"},
        {**ruleset(value=False), "target": "tag"},
        {k: v for k, v in ruleset(value=False).items() if k != "bypass_actors"},
    ],
)
def test_extra_changes(body: dict[str, object]) -> None:
    assert approved_decisions(PATH, CURRENT, body, [APPROVAL]) == ()


def test_empty_list_refuses_the_change() -> None:
    assert approved_decisions(PATH, CURRENT, ruleset(value=False), []) == ()


@pytest.mark.parametrize("missing", ["bypass_actors", "source_type"])
def test_incomplete_read(missing: str) -> None:
    current = {key: value for key, value in CURRENT.items() if key != missing}
    assert approved_decisions(PATH, current, ruleset(value=False), [APPROVAL]) == ()
