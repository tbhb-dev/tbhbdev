# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request draft."""

import pytest
from unit.alpha.pull_request.pr_draft_samples import (
    BASE,
    SECTIONS,
    replace_once,
    structure,
)

from tbhbdev.alpha.identity import dispatcher as agent_identity
from tbhbdev.alpha.pull_request import body as pr_body


def test_minimal_draft_passes() -> None:
    assert structure(BASE) == []


WHY_61 = " ".join(["word"] * 61)
WHY_60 = " ".join(["word"] * 60)


CASES: list[tuple[str, str, list[pr_body.Problem]]] = [
    ("empty", "", [(1, "the first line after the frontmatter must be the H1 title")]),
    (
        "section first",
        "## Why\n\nw\n",
        [(1, "the first line after the frontmatter must be the H1 title")],
    ),
    (
        "code first",
        "```\nx\n```\n" + BASE,
        [(1, "the first line after the frontmatter must be the H1 title")],
    ),
    (
        "missing why",
        replace_once(BASE, "## Why\n\nw\n\n", ""),
        [(1, "missing required section 'Why'")],
    ),
    (
        "missing verification",
        replace_once(BASE, "\n## Verification\n\nv\n", ""),
        [(1, "missing required section 'Verification'")],
    ),
    (
        "unknown",
        BASE + "\n## Provenance\n\nx\n",
        [
            (
                15,
                "unknown section 'Provenance'; use: "
                + ", ".join(s.heading for s in SECTIONS),
            )
        ],
    ),
    (
        "out of order",
        BASE + "\n## Risk\n\nx\n",
        [(15, "section 'Risk' is duplicated or out of order")],
    ),
    (
        "duplicate",
        BASE + "\n## Verification\n\nagain\n",
        [(15, "section 'Verification' is duplicated or out of order")],
    ),
    (
        "empty section",
        BASE + "\n## Human steering\n",
        [(15, "section 'Human steering' is empty")],
    ),
    (
        "comment only section",
        BASE + "\n## Human steering\n\n<!-- later -->\n",
        [
            (17, "HTML comment left in the description"),
            (15, "section 'Human steering' is empty"),
        ],
    ),
    (
        "comment",
        replace_once(BASE, "\nw\n", "\nw <!-- hi -->\n"),
        [(5, "HTML comment left in the description")],
    ),
    (
        "second h1",
        BASE + "\n# Again\n",
        [(15, "only one H1 is allowed: the title")],
    ),
    (
        "stray text",
        replace_once(BASE, "# fix: x\n\n", "# fix: x\n\nstray words\n\n"),
        [(3, "text before the first section; put it under a heading")],
    ),
    (
        "unclosed fence",
        BASE + "\n```\ncode\n",
        [(15, "code fence is never closed")],
    ),
    (
        "limit",
        replace_once(BASE, "\nw\n", f"\n{WHY_61}\n"),
        [(3, "section 'Why' has 61 words (limit 60)")],
    ),
    (
        "trailer",
        BASE + "\nGenerated-by: LLM harness=claude-code\n",
        [(15, "the script writes Generated-by and Assisted-by; remove this line")],
    ),
    (
        "assisted trailer",
        BASE + "Assisted-by: LLM harness=codex\n",
        [(14, "the script writes Generated-by and Assisted-by; remove this line")],
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"), [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES]
)
def test_structure_problems(text: str, expected: list[pr_body.Problem]) -> None:
    assert sorted(structure(text)) == sorted(expected)


def test_limit_boundary() -> None:
    assert structure(replace_once(BASE, "\nw\n", f"\n{WHY_60}\n")) == []


def test_code_and_links_are_free() -> None:
    text = replace_once(
        BASE,
        "\nw\n",
        f"\n{WHY_60}\n\n```\n{WHY_61}\n```\n\n`{WHY_61}` https://example.com/a\n",
    )
    assert structure(text) == []


def test_code_only_section_is_not_empty() -> None:
    assert structure(replace_once(BASE, "\nv\n", "\n```\n$ go test\n```\n")) == []


def test_fenced_lines_are_not_headings_comments_or_trailers() -> None:
    fenced = "\n```\n## Not a section\n<!-- not a comment -->\nAssisted-by: x\n```\n"
    assert structure(BASE + fenced) == []


def test_h3_and_indented_headings() -> None:
    text = replace_once(BASE, "\nw\n", "\n### Detail\n\nw\n")
    assert structure(text) == []
    assert structure(replace_once(BASE, "## Why", "   ## Why")) == []
    assert structure(replace_once(BASE, "## Why", "    ## Why")) == [
        (1, "missing required section 'Why'"),
        (3, "text before the first section; put it under a heading"),
    ]


def test_title_may_follow_blank_lines() -> None:
    assert structure("\n\n" + BASE) == []
    assert structure("---\nbase: main\n---\n\n" + BASE) == []


@pytest.mark.parametrize("key", ["Signed-off-by", "Co-authored-by", "co-authored-by"])
def test_human_trailers_are_rejected_for_everyone(key: str) -> None:
    text = BASE + f"\n{key}: A <a@example.com>\n"
    assert structure(text) == [(15, pr_body.HUMAN_TRAILER_MESSAGE)]


def test_line_numbers_follow_the_frontmatter() -> None:
    text = "---\nbase: main\n---\n\n" + replace_once(BASE, "\nw\n", "\nw <!-- c -->\n")
    assert structure(text) == [(9, "HTML comment left in the description")]


def value(**attrs: str) -> str:
    return agent_identity.format_value(attrs)


def test_unknown_section_does_not_hide_later_problems() -> None:
    text = "# fix: x\n\n## Nope\n\nx\n\n## Why\n\n"
    got = structure(text)
    known = (
        "Why, Behavior change, Decisions, Deviations from the task, "
        "Needs human judgment, Risk, Verification, Human steering"
    )
    assert (3, f"unknown section 'Nope'; use: {known}") in got
    assert (7, "section 'Why' is empty") in got


def test_code_lines_do_not_hide_later_trailers() -> None:
    text = BASE.replace("v\n", "v\n\n```\ncode\n```\n\nGenerated-by: LLM x\n")
    assert (
        19,
        "the script writes Generated-by and Assisted-by; remove this line",
    ) in structure(text)


def test_trailer_right_after_the_title_is_found() -> None:
    text = "# fix: x\nGenerated-by: LLM x\n\n## Why\n\nw\n"
    assert (
        2,
        "the script writes Generated-by and Assisted-by; remove this line",
    ) in structure(text)


def test_a_non_title_first_line_is_reported_where_it_is() -> None:
    assert (
        2,
        "the first line after the frontmatter must be the H1 title",
    ) in structure("\nplain\n")


def ref(no: int, number: int) -> pr_body.Problem:
    return (no, pr_body.BARE_REF_MESSAGE.format(number=number))


def test_a_bare_reference_fails_the_check_in_the_title_and_body() -> None:
    text = BASE.replace("# fix: x\n", "# fix: x (#1)\n").replace("\nw\n", "\nsee #2\n")
    found = structure(text)
    assert ref(1, 1) in found
    assert ref(5, 2) in found


def test_a_wrapped_draft_fails_the_check() -> None:
    text = BASE.replace("\nw\n", "\nline one\nline two\n")
    assert (5, pr_body.WRAPPED) in structure(text)


@pytest.mark.parametrize(
    "line",
    [
        "Opened-by: LLM harness=codex",
        "opened-by: x",
        "  Opened-By : x",
        "OPENED-BY:",
    ],
)
def test_check_rejects_an_opened_by_line(line: str) -> None:
    assert structure(BASE + f"\n{line}\n") == [(15, pr_body.OPENED_MESSAGE)]


def test_check_rejects_opened_by_in_code_too() -> None:
    text = BASE + "\n```text\nOpened-by: x\n```\n"
    assert structure(text) == [(16, pr_body.OPENED_MESSAGE)]
    assert structure(BASE + "\nSee the Opened-by: line.\n") == []
