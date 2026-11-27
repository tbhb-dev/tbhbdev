# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for scanning pull request Markdown."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tbhbdev.pull_request import markdown

# -------------------------------------------------------------- word counting


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
    assert markdown.count_words(text) == count


WORD = st.text(alphabet="abcdefghij", min_size=1, max_size=6)


@given(st.lists(WORD, max_size=30))
@settings(database=None)
def test_count_words_counts_words(words: list[str]) -> None:
    assert markdown.count_words(" ".join(words)) == len(words)


@given(st.lists(WORD, max_size=10), WORD, WORD)
@settings(database=None)
def test_free_text_adds_nothing(words: list[str], inner: str, url: str) -> None:
    text = " ".join(words)
    free = [
        f"`{inner} {inner}`",
        f"<!-- {inner} -->",
        f"<https://example.com/{url}>",
        f"https://example.com/{url}",
    ]
    assert markdown.count_words(text + " " + " ".join(free)) == len(words)
    assert markdown.count_words(f"[{inner}](https://example.com/{url})") == 1


# ----------------------------------------------------------- lines and fences


def test_fence_flags() -> None:
    raw = ["a", "```py", "x", "```", "b", "~~~", "y", "```", "~~~~", "c"]
    flags, unclosed = markdown.fence_flags(raw)
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
    assert markdown.fence_flags(raw)[1] == unclosed


def test_strip_comments_keeps_line_numbers() -> None:
    texts = ["a <!-- x", "y --> b", "c", "<!-- z -->"]
    clean, starts = markdown.strip_comments(texts)
    assert clean == ["a ", " b", "c", ""]
    assert starts == [0, 3]
    assert markdown.strip_comments([]) == ([], [])
    assert markdown.strip_comments(["a <!-- open", "b"])[0] == ["a ", ""]


def test_scan_reports_comments_and_fences() -> None:
    lines, problems = markdown.scan(["a", "<!-- c -->", "```", "<!-- no -->", "x"], 10)
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
    assert markdown.heading_of(markdown.Line(1, text, text, code=False)) == found


def test_heading_of_ignores_code() -> None:
    assert markdown.heading_of(markdown.Line(1, "## x", "", code=True)) is None
    assert markdown.heading_of(markdown.Line(1, "## x", "## x", code=True)) is None


@given(st.lists(st.sampled_from(["a", "", "x y", "<!-- c -->"]), max_size=12))
@settings(database=None)
def test_scan_keeps_one_line_per_input(raw: list[str]) -> None:
    lines, _ = markdown.scan(raw, 5)
    assert [line.no for line in lines] == list(range(5, 5 + len(raw)))
    assert [line.raw for line in lines] == raw


def test_split_blocks_preserves_the_preamble_and_code() -> None:
    raw = ["", "intro", "## Why", "text", "# extra title", "```", "## code", "```"]
    lines, _ = markdown.scan(raw, 4)
    preamble, blocks, problems = markdown.split_blocks(lines)
    assert preamble == lines[:2]
    assert blocks == [markdown.Block("Why", 6, lines[3:])]
    assert problems == [(8, "only one H1 is allowed: the title")]
    assert [markdown.is_blank(line) for line in lines] == [True] + [False] * 7
