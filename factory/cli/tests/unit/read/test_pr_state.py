"""Tests for the pull request state field values and their escaping."""

import pytest

from tbhbdev.read import pr_state

SHA = "0123456789abcdef0123456789abcdef01234567"
# The REST shape of `GET /repos/{owner}/{repo}/pulls/{number}`, cut to what is read.
SAMPLE: dict[str, object] = {
    "number": 7,
    "state": "open",
    "draft": False,
    "mergeable_state": "clean",
    "title": "Fix the thing",
    "user": {"login": "octo", "id": 1},
    "head": {"sha": SHA, "ref": "topic"},
}


def test_fields_and_order() -> None:
    assert tuple(pr_state.PATHS) == pr_state.DEFAULT
    assert pr_state.select(SAMPLE, pr_state.DEFAULT) == [
        SHA,
        "open",
        "false",
        "clean",
        "octo",
        "Fix the thing",
    ]
    assert pr_state.select(SAMPLE, ["title", "head_sha"]) == ["Fix the thing", SHA]
    assert pr_state.select({"draft": True}, ["draft"]) == ["true"]


def test_null_and_absent_values() -> None:
    sample = {**SAMPLE, "user": None, "mergeable_state": None, "head": None}
    names = ["head_sha", "state", "merge_state", "author"]
    assert pr_state.select(sample, names) == [None, "open", None, None]


@pytest.mark.parametrize(
    "data", [[], {"state": 3}, {"draft": "no"}, {"user": "o"}, {"head": {"sha": []}}]
)
def test_malformed(data: object) -> None:
    with pytest.raises(pr_state.MalformedError):
        pr_state.select(data, pr_state.DEFAULT)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "none"),
        ("none", "\\none"),
        ("", ""),
        ("a\tb", "a\\tb"),
        ("a\nb\r\nc", "a\\nb\\r\\nc"),
        ("a\\b", "a\\\\b"),
        ("a\\none", "a\\\\none"),
        ("a" + chr(0x2028) + "b\x0bc", "a\\u2028b\\u000bc"),
    ],
)
def test_escape(value: str | None, text: str) -> None:
    assert pr_state.escape(value) == text
