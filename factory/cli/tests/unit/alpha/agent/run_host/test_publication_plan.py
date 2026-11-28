"""Check publication decisions using recorded branches and API values."""

import pytest

from tbhbdev.alpha.agent.run_host import publication_plan

REF = "refs/heads/agent/sample"


@pytest.mark.parametrize(
    ("start_ref", "branch"),
    [(None, "agent/sample"), ("refs/heads/fix/topic", "fix/topic")],
)
def test_branch_name(start_ref: str | None, branch: str) -> None:
    assert publication_plan.branch_name("sample", start_ref) == branch


@pytest.mark.parametrize(
    ("ref", "sha", "expected"),
    [(REF, "a" * 40, "a" * 40), (REF, "bad", None), (f"{REF}-other", "a" * 40, None)],
)
def test_ref_tip(ref: str, sha: str, expected: str | None) -> None:
    assert (
        publication_plan.ref_tip({"ref": ref, "object": {"sha": sha}}, REF) == expected
    )


@pytest.mark.parametrize(
    ("record", "expected"),
    [({"status": "ahead"}, True), ({"status": "diverged"}, False), ([], None)],
)
def test_origin_ahead(record: object, expected: bool | None) -> None:
    assert publication_plan.origin_ahead(record) is expected
