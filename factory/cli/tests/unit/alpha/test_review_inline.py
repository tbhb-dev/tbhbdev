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
        review_inline.Finding(
            ("cache.py", 11), f"- {IN_DIFF}\n", 13, IN_DIFF.replace("`", "")
        )
    ]


def test_anchors_use_the_judged_side_and_current_filename() -> None:
    rows = "\n".join(
        f"- F{i}: P2 correctness See `{location}`."
        for i, location in enumerate(
            (
                "cache.py:10",
                "cache.py:15",
                "cache.py:21",
                "old_cache.py:11",
                "image.png:1",
            ),
            1,
        )
    )
    found = review_inline.findings(f"## Findings\n\n{rows}\n")
    assert review_inline.split_findings(found, DIFF) == (found[:2], found[2:])


def test_count_line_groups_findings_by_priority_then_kind() -> None:
    draft = (
        "## Findings\n\n"
        "- F1: P2 design Replace the flag. `cache.py:10`\n"
        "- **F2:** P1 security The cache exposes values. `cache.py:11`\n"
        "- F3: P2 correctness The cache stores different values. `cache.py:15`\n"
        "- F4: P2 design Remove the extra wrapper. `cache.py:15`\n"
    )
    body, comments = review_inline.render(draft, review_inline.findings(draft), DIFF)
    assert body == (
        "## Findings\n\n"
        "Inline findings: 4 (P1 security 1, P2 correctness 1, P2 design 2).\n"
        "F1 to F4.\n"
    )
    assert [str(comment["body"]).split(":", 1)[0] for comment in comments] == [
        "F1",
        "F2",
        "F3",
        "F4",
    ]
