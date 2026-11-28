"""Policy rules for archive-only repository writes and protected repositories."""

# cspell:words bhbdev adget

import pytest

from tbhbdev.admin import policy

ARCHIVE: dict[str, object] = {"archived": True}


def test_archive_only_patch_is_allowed() -> None:
    assert policy.check("PATCH", "/repos/tbhb-dev/widget", ARCHIVE) == policy.Decision(
        allowed=True,
        code="allowed",
        path="/repos/tbhb-dev/widget",
        permission="administration",
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/repos/tbhb-dev/widget", {"archived": False}),
        ("PATCH", "/repos/tbhb-dev/widget", {"archived": "true"}),
        ("PATCH", "/repos/tbhb-dev/widget", {"archived": 1}),
        ("PATCH", "/repos/tbhb-dev/widget", {"archived": None}),
        ("PATCH", "/repos/tbhb-dev/widget", {"archived": True, "description": "x"}),
        ("PATCH", "/repos/tbhb-dev/widget", {"archived": True, "has_issues": False}),
        ("PUT", "/repos/tbhb-dev/widget", ARCHIVE),
        ("DELETE", "/repos/tbhb-dev/widget", None),
        ("POST", "/repos/tbhb-dev/widget/transfer", {"new_owner": "other"}),
    ],
)
def test_repository_removal_variants_are_refused(
    method: str, path: str, body: object
) -> None:
    assert policy.check(method, path, body) == policy.Decision(
        allowed=False, code="repository_removal", path=policy.normalize(path)
    )


@pytest.mark.parametrize(
    "path",
    [
        "/repos/tbhb-dev/tbhbdev",
        "/REPOS/TBHB-DEV/TBHBDEV",
        "/repos/tbhb-dev/%74bhbdev",
    ],
)
def test_self_repository_cannot_be_archived(path: str) -> None:
    assert policy.check("PATCH", path, ARCHIVE) == policy.Decision(
        allowed=False, code="repository_removal", path="/repos/tbhb-dev/tbhbdev"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/repos/tbhb-dev/gadget",
        "/REPOS/TBHB-DEV/GADGET",
        "/repos/tbhb-dev/%67adget",
    ],
)
def test_protected_repository_cannot_be_archived(path: str) -> None:
    protected = frozenset({"tbhb-dev/gadget"})
    assert policy.check("PATCH", path, ARCHIVE, protected=protected) == policy.Decision(
        allowed=False, code="repository_removal", path="/repos/tbhb-dev/gadget"
    )


def test_unprotected_repository_may_be_archived_when_protected_is_empty() -> None:
    assert policy.check(
        "PATCH", "/repos/tbhb-dev/gadget", ARCHIVE, protected=frozenset()
    ) == policy.Decision(
        allowed=True,
        code="allowed",
        path="/repos/tbhb-dev/gadget",
        permission="administration",
    )


def test_malformed_protected_list_refuses_archive_only() -> None:
    assert policy.check(
        "PATCH", "/repos/tbhb-dev/widget", ARCHIVE, protected=None
    ) == policy.Decision(
        allowed=False, code="repository_removal", path="/repos/tbhb-dev/widget"
    )
    assert policy.check(
        "PATCH", "/repos/tbhb-dev/widget", {"description": "x"}, protected=None
    ).allowed


def test_foreign_owner_archive_is_refused_as_foreign_owner() -> None:
    assert policy.check("PATCH", "/repos/other-org/widget", ARCHIVE) == policy.Decision(
        allowed=False, code="foreign_owner", path="/repos/other-org/widget"
    )


def test_non_archive_write_to_self_repository_still_passes() -> None:
    assert policy.check(
        "PATCH", "/repos/tbhb-dev/tbhbdev", {"description": "x"}
    ).allowed
