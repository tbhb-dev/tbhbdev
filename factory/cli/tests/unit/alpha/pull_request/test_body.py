# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request body."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from unit.alpha.pull_request.pr_draft_samples import GEN_LINE, SECTIONS

from tbhbdev.alpha.identity.commit.grammar import LINE
from tbhbdev.alpha.pull_request import body as pr_body


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("", 0),
        ("one two  three\nfour", 4),
        ("- a\n- b", 2),
        ("-- ... *", 0),
        ("`code words` plain", 1),
        ("``a ` b`` plain", 1),
        ("[link text](https://example.com/a b) after", 3),
        ("![alt text](img.png)", 2),
        ("[text][ref] more\n\n[ref]: https://example.com", 2),
        ("<https://example.com> see https://example.com/x now", 2),
        ("a <!-- hidden words --> b", 2),
        ("a <!-- never closed\nhidden", 1),
        ("co-op don't 3rd", 3),
    ],
)
def test_count_words(text: str, count: int) -> None:
    assert pr_body.count_words(text) == count


WORD = st.text(alphabet="abcdefghij", min_size=1, max_size=6)


@given(st.lists(WORD, max_size=30))
def test_count_words_counts_words(words: list[str]) -> None:
    assert pr_body.count_words(" ".join(words)) == len(words)


@given(st.lists(WORD, max_size=10), WORD, WORD)
def test_free_text_adds_nothing(words: list[str], inner: str, url: str) -> None:
    text = " ".join(words)
    free = [
        f"`{inner} {inner}`",
        f"<!-- {inner} -->",
        f"<https://example.com/{url}>",
        f"https://example.com/{url}",
    ]
    assert pr_body.count_words(text + " " + " ".join(free)) == len(words)
    assert pr_body.count_words(f"[{inner}](https://example.com/{url})") == 1


def test_fence_flags() -> None:
    raw = ["a", "```py", "x", "```", "b", "~~~", "y", "```", "~~~~", "c"]
    flags, unclosed = pr_body.fence_flags(raw)
    assert flags == [False, True, True, True, False, True, True, True, True, False]
    assert unclosed is None


@pytest.mark.parametrize(
    ("raw", "unclosed"),
    [
        (["a", "```", "x"], 1),
        (["```", "x", "``` info", "y"], 0),
        (["````", "x", "```", "y"], 0),
        (["   ```", "x", "   ```"], None),
        (["    ```", "x"], None),
        (["~~~", "```", "~~~"], None),
    ],
)
def test_fence_unclosed(raw: list[str], unclosed: int | None) -> None:
    assert pr_body.fence_flags(raw)[1] == unclosed


def test_strip_comments_keeps_line_numbers() -> None:
    texts = ["a <!-- x", "y --> b", "c", "<!-- z -->"]
    clean, starts = pr_body.strip_comments(texts)
    assert clean == ["a ", " b", "c", ""]
    assert starts == [0, 3]
    assert pr_body.strip_comments([]) == ([], [])
    assert pr_body.strip_comments(["a <!-- open", "b"])[0] == ["a ", ""]


def test_scan_reports_comments_and_fences() -> None:
    lines, problems = pr_body.scan(["a", "<!-- c -->", "```", "<!-- no -->", "x"], 10)
    assert problems == [
        (11, "HTML comment left in the description"),
        (12, "code fence is never closed"),
    ]
    assert [(line.no, line.text, line.code) for line in lines] == [
        (10, "a", False),
        (11, "", False),
        (12, "", True),
        (13, "", True),
        (14, "", True),
    ]
    assert lines[1].raw == "<!-- c -->"


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("# T", (1, "T")),
        ("## Two words ", (2, "Two words")),
        ("###### six", (6, "six")),
        ("   ## indented", (2, "indented")),
        ("    ## code", None),
        ("#nospace", None),
        ("####### seven", None),
        ("plain", None),
        ("#", None),
    ],
)
def test_heading_of(text: str, found: tuple[int, str] | None) -> None:
    assert pr_body.heading_of(pr_body.Line(1, text, text, code=False)) == found


def test_heading_of_ignores_code() -> None:
    assert pr_body.heading_of(pr_body.Line(1, "## x", "", code=True)) is None
    assert pr_body.heading_of(pr_body.Line(1, "## x", "## x", code=True)) is None


@given(st.lists(st.sampled_from(["a", "", "x y", "<!-- c -->"]), max_size=12))
def test_scan_keeps_one_line_per_input(raw: list[str]) -> None:
    lines, _ = pr_body.scan(raw, 5)
    assert [line.no for line in lines] == list(range(5, 5 + len(raw)))
    assert [line.raw for line in lines] == raw


def sections_text(names: list[str], bodies: list[str]) -> str:
    return "".join(f"## {n}\n\n{b}\n\n" for n, b in zip(names, bodies, strict=True))


@given(
    st.lists(
        st.tuples(st.integers(0, len(SECTIONS) - 1), st.integers(1, 40)),
        unique_by=lambda pair: pair[0],
        max_size=len(SECTIONS),
    )
)
def test_block_problems_follow_the_scheme(chosen: list[tuple[int, int]]) -> None:
    chosen = sorted(chosen)
    names = [SECTIONS[index].heading for index, _ in chosen]
    bodies = [" ".join(["word"] * count) for _, count in chosen]
    lines, _ = pr_body.scan(sections_text(names, bodies).splitlines(), 1)
    _, blocks, _ = pr_body.split_blocks(lines)
    assert [block.heading for block in blocks] == names
    found = pr_body.block_problems(blocks, SECTIONS)
    over = [
        n
        for (index, count), n in zip(chosen, names, strict=True)
        if count > (SECTIONS[index].max_words or 0)
    ]
    assert [text.split("'")[1] for _, text in found] == over


@given(st.permutations(range(len(SECTIONS))))
def test_block_problems_flag_any_other_order(order: list[int]) -> None:
    names = [SECTIONS[index].heading for index in order]
    lines, _ = pr_body.scan(sections_text(names, ["x"] * len(names)).splitlines(), 1)
    _, blocks, _ = pr_body.split_blocks(lines)
    found = pr_body.block_problems(blocks, SECTIONS)
    assert (len(found) > 0) == (order != sorted(order))


@pytest.mark.parametrize(
    ("body", "lines"),
    [
        ("One line.\n\nNext paragraph.\n", []),
        ("First line\nsecond line\n", [1]),
        ("a\nb\nc\n\nd\ne\n", [1, 5]),
        ("- item one\n- item two\n", []),
        ("- item one\n  continues\n- item two\n", [1]),
        ("1. one\n2) two\n", []),
        ("Intro:\n- a\n- b\n", []),
        ("text\n```\ncode\nmore\n```\ntail\n", []),
        ("| a | b |\n| - | - |\n| 1 | 2 |\n", []),
        ("### Heading\ntext\n", []),
        ("text\n<!-- note -->\nmore\n", []),
        (f"text\n{GEN_LINE}\n", []),
        ("text\nSigned-off-by: A <a@b.c>\n", []),
    ],
)
def test_wrap_problems(body: str, lines: list[int]) -> None:
    scanned, _ = pr_body.scan(body.splitlines(), 1)
    assert pr_body.wrap_problems(scanned, LINE) == [
        (no, pr_body.WRAPPED) for no in lines
    ]


def ref(no: int, number: int) -> pr_body.Problem:
    return (no, pr_body.BARE_REF_MESSAGE.format(number=number))


def test_the_bare_reference_message_names_the_fix() -> None:
    assert pr_body.BARE_REF_MESSAGE.format(number=7) == (
        "#7 links to this repository's #7; write owner/repo#7, even for this repository"
    )


@pytest.mark.parametrize(
    ("body", "problems"),
    [
        ("See #12.\n", [ref(1, 12)]),
        ("(#3) and #4\n", [ref(1, 3), ref(1, 4)]),
        ("- item #7\n", [ref(1, 7)]),
        ("tbhb-dev/tbhbdev#4 and tbhb-dev/renovate-config#2\n", []),
        ("x.y#5 and a-b#6 and &#38; and path/#8\n", []),
        ("`#9` in code\n", []),
        ("```\n#10\n```\n", []),
        ("[#11](https://example.com/11) and [#12][r]\n", []),
        ("https://github.com/o/r/pull/13#14\n", []),
        ("<!-- #15 -->\n", []),
        ("## Why\n#16 here\n", [ref(2, 16)]),
        ("issue#17 and #x and #\n", []),
    ],
)
def test_ref_problems(body: str, problems: list[pr_body.Problem]) -> None:
    scanned, _ = pr_body.scan(body.splitlines(), 1)
    assert pr_body.ref_problems(scanned) == problems
