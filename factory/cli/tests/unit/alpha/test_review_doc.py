"""Exercise review submission with an injected diff and poster."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

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
WHOLE = "F3: P3 test The smoke run covered only empty records."
UNANCHORED = " (not anchored: line not in the diff)"
BODY = (
    "## Summary\n\nThe cache can retain stale records.\n\n"
    "## Findings\n\n{findings}\n\n"
    "## Verification\n\n- Read the cache and its callers in `cache.py:10`.\n\n"
    "Generated-by: LLM harness=codex;model=m1\n"
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
    ("findings", "sent", "extra", "lookups"),
    [
        (
            f"- {IN_DIFF}\n- {OUTSIDE_DIFF}\n- {WHOLE}",
            (
                "Inline findings: 1 (P2 correctness 1).\nF3.\n\n"
                f"- {OUTSIDE_DIFF.replace('F2:', 'F1:')}{UNANCHORED}\n"
                f"- {WHOLE.replace('F3:', 'F2:')}"
            ),
            {
                "comments": [
                    {
                        "path": "cache.py",
                        "line": 11,
                        "side": "RIGHT",
                        "body": IN_DIFF.replace("F1:", "F3:"),
                    }
                ]
            },
            [("GET", PULL), ("GET", COMPARE)],
        ),
        (
            f"- {WHOLE.replace('F3:', 'F1:')}",
            f"- {WHOLE.replace('F3:', 'F1:')}",
            {},
            [],
        ),
        (
            f"- {OUTSIDE_DIFF.replace('F2:', 'F1:')}",
            f"- {OUTSIDE_DIFF.replace('F2:', 'F1:')}{UNANCHORED}",
            {},
            [("GET", PULL), ("GET", COMPARE)],
        ),
    ],
)
def test_submit_review(
    findings: str,
    sent: str,
    extra: dict[str, object],
    lookups: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = BODY.format(findings=findings)
    posted: list[dict[str, object]] = []
    check = Mock(return_value=[])
    monkeypatch.setattr(review_doc, "check_review", check)

    def poster(payload: dict[str, object]) -> object:
        posted.append(payload)
        return {"id": 17}

    api = ReviewApi(DIFF, poster)
    review = review_doc.ReviewSubmission(PULL, JUDGED, body, "APPROVE")
    assert review_doc.send_review(api, review) == 17
    assert api.calls == [*lookups, ("POST", f"{PULL}/reviews")]
    sent_body = BODY.format(findings=sent)
    check.assert_called_once_with(
        sent_body, metadata=None, event="APPROVE", posted=True
    )
    assert posted == [
        {
            "commit_id": JUDGED,
            "body": sent_body,
            "event": "APPROVE",
            **extra,
        }
    ]


@pytest.mark.parametrize(
    "diff",
    [
        pytest.param({}, id="missing-files"),
        review_doc.ReviewError("comparison unavailable"),
    ],
)
def test_submit_review_when_inline_comments_cannot_be_built(
    diff: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = BODY.format(findings=f"- {IN_DIFF}\n- {OUTSIDE_DIFF}")
    monkeypatch.setattr(review_doc, "check_review", Mock(return_value=[]))

    def poster(payload: dict[str, object]) -> object:
        expected = BODY.format(
            findings=f"- {IN_DIFF}{UNANCHORED}\n- {OUTSIDE_DIFF}{UNANCHORED}"
        )
        assert payload == {"commit_id": JUDGED, "body": expected, "event": "COMMENT"}
        return {"id": 17}

    api = ReviewApi(diff, poster)
    review = review_doc.ReviewSubmission(PULL, JUDGED, body, "COMMENT")
    assert review_doc.send_review(api, review) == 17
    assert api.calls == [("GET", PULL), ("GET", COMPARE), ("POST", f"{PULL}/reviews")]
    notice = capsys.readouterr().err
    if isinstance(diff, review_doc.ReviewError):
        assert notice == "inline comments skipped: comparison unavailable\n"
    else:
        assert notice == ""
