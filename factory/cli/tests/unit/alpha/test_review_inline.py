"""Exercise review finding citations and their diff anchors."""

from tbhbdev.alpha import review_inline

IN_DIFF = "F1: P2 correctness The cache retains stale records. `cache.py:11`"
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


def test_findings_at_end_of_draft() -> None:
    body = f"## Findings\n\n- {IN_DIFF}\n"
    assert review_inline.findings(body) == [
        review_inline.Finding("cache.py", 11, IN_DIFF)
    ]


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
