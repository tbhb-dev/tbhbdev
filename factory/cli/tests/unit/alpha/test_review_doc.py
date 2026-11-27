"""Exercise review submission with an injected diff and poster."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import review_doc

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
            "patch": "@@ -20,2 +10,2 @@\n context\n-old\n+new",
        },
    ],
}


class ReviewApi:
    def __init__(
        self,
        diff: object,
        poster: Callable[[dict[str, object]], object],
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
            if isinstance(self.diff, review_doc.ReviewError):
                raise self.diff
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


@pytest.mark.parametrize(
    "diff",
    [
        pytest.param({}, id="missing-files"),
        review_doc.ReviewError("comparison unavailable"),
    ],
)
def test_submit_review_when_inline_comments_cannot_be_built(
    diff: object, capsys: pytest.CaptureFixture[str]
) -> None:
    body = BODY.format(findings=f"- {IN_DIFF}")

    def poster(payload: dict[str, object]) -> object:
        assert payload == {"commit_id": JUDGED, "body": body, "event": "COMMENT"}
        return {"id": 17}

    api = ReviewApi(diff, poster)
    assert review_doc.send_review(api, PULL, JUDGED, body, "COMMENT") == 17
    assert api.calls == [("GET", PULL), ("GET", COMPARE), ("POST", f"{PULL}/reviews")]
    notice = capsys.readouterr().err
    if isinstance(diff, review_doc.ReviewError):
        assert notice == "inline comments skipped: comparison unavailable\n"
    else:
        assert notice == ""
