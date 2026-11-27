"""Exercise review submission with an injected diff and poster."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import review_doc, review_inline

if TYPE_CHECKING:
    from collections.abc import Callable

BASE = "b" * 40
JUDGED = "a" * 40
REPO = "repos/owner/project"
PULL = f"{REPO}/pulls/7"
COMPARE = f"{REPO}/compare/{BASE}...{JUDGED}?per_page=1"
IN_DIFF = "F1: P2 correctness The cache retains stale records. `cache.py:11`"
OUTSIDE_DIFF = "F2: P3 docs The example omits a required option. `cache.py:12`"
BODY = (
    "## Summary\n\nThe cache can retain stale records.\n\n"
    "## Findings\n\n{findings}\n\n"
    "## Verification\n\n- Read the cache and its callers in `cache.py:10`.\n"
)
DIFF = {
    "files": [
        {
            "filename": "cache.py",
            "previous_filename": "old_cache.py",
            "patch": (
                "@@ -20,2 +10,2 @@\n context\n-old\n+new\n"
                "\\ No newline at end of file\n@@ -25 +15 @@\n-old\n+new"
            ),
        },
        {"filename": "image.png"},
    ],
}


class ReviewApi:
    def __init__(
        self, diff: object, poster: Callable[[dict[str, object]], object]
    ) -> None:
        self.diff = diff
        self.poster = poster
        self.calls: list[tuple[str, str]] = []

    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object:
        self.calls.append((method, path))
        if (method, path) == ("GET", PULL):
            return {"base": {"sha": BASE}, "head": {"sha": "c" * 40}}
        if (method, path) == ("GET", COMPARE):
            return self.diff
        assert (method, path) == ("POST", f"{PULL}/reviews")
        assert payload is not None
        return self.poster(payload)


@pytest.mark.parametrize(
    ("findings", "extra", "lookups"),
    [
        (
            f"- {IN_DIFF}\n- {OUTSIDE_DIFF}",
            {
                "comments": [
                    {"path": "cache.py", "line": 11, "side": "RIGHT", "body": IN_DIFF}
                ]
            },
            [("GET", PULL), ("GET", COMPARE)],
        ),
        ("- F1: P3 test The cache test covers only empty records.", {}, []),
        (
            f"- {OUTSIDE_DIFF.replace('F2:', 'F1:')}",
            {},
            [("GET", PULL), ("GET", COMPARE)],
        ),
    ],
)
def test_submit_review(
    findings: str, extra: dict[str, object], lookups: list[tuple[str, str]]
) -> None:
    body = BODY.format(findings=findings)
    posted: list[dict[str, object]] = []

    def poster(payload: dict[str, object]) -> object:
        posted.append(payload)
        return {"id": 17}

    api = ReviewApi(DIFF, poster)
    assert review_doc.send_review(api, PULL, JUDGED, body, "APPROVE") == 17
    assert api.calls == [*lookups, ("POST", f"{PULL}/reviews")]
    assert posted == [{"commit_id": JUDGED, "body": body, "event": "APPROVE", **extra}]


def test_anchors_use_the_judged_side_and_current_filename() -> None:
    found = [
        review_inline.Finding("cache.py", 10, "context"),
        review_inline.Finding("cache.py", 15, "next hunk"),
        review_inline.Finding("cache.py", 21, "deleted line"),
        review_inline.Finding("old_cache.py", 11, "old name"),
        review_inline.Finding("image.png", 1, "binary file"),
    ]
    assert review_inline.comments(found, DIFF) == [
        {"path": "cache.py", "line": 10, "side": "RIGHT", "body": "context"},
        {"path": "cache.py", "line": 15, "side": "RIGHT", "body": "next hunk"},
    ]
