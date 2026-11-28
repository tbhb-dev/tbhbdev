# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pure tests for the plain-text body renderer."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from tbhbdev.alpha.pull_request.body import (
    FENCE,
    INLINE_CODE,
    heading_of,
    scan,
    split_blocks,
)
from tbhbdev.merge.body import (
    TOKEN,
    WIDTH,
    Line,
    Syntax,
    render,
    render_body,
    wrap,
)


def body_text(body: str) -> str:
    lines, _ = scan(body.splitlines(), 1)
    preamble, blocks, _ = split_blocks(lines)
    sections = [("", preamble), *((block.heading, block.lines) for block in blocks)]
    parts = render_body(sections, Syntax(FENCE, INLINE_CODE, heading_of))
    return "\n\n".join(parts) + "\n" if parts else ""


def message_of(body: str) -> str:
    text = body_text(body)
    return "fix: x (#7)\n" + ("\n" + text if text else "")


WORD = st.text(
    st.characters(categories=("L", "N", "P", "S"), exclude_characters="`"),
    min_size=1,
    max_size=90,
)


SPAN = st.builds(
    "`{}`".format,
    st.text("ab -=()", min_size=1, max_size=30).filter(
        lambda text: text.strip() == text and text
    ),
)


TEXT = st.lists(st.one_of(WORD, SPAN), min_size=1, max_size=60).map(" ".join)


PREFIX = st.sampled_from([("", ""), ("- ", "  "), ("12. ", "    "), ("  * ", "    ")])


LONG_CELL = "the check " + "ran again " * 10 + "and passed"


@given(text=TEXT, prefix=PREFIX)
def test_wrap_keeps_every_token_in_order_within_the_limit(
    text: str, prefix: tuple[str, str]
) -> None:
    lines = wrap(text, *prefix)
    tokens = [match[0] for match in TOKEN.finditer(text)]
    bare = [
        line.removeprefix(prefix[0] if i == 0 else prefix[1])
        for i, line in enumerate(lines)
    ]
    assert [m[0] for m in TOKEN.finditer("\n".join(bare))] == tokens
    for line in lines:
        assert len(line) <= WIDTH or len(TOKEN.findall(line)) == 1
    assert lines[0].startswith(prefix[0])
    assert all(line.startswith(prefix[1]) for line in lines[1:])


def test_wrap_never_splits_a_long_token() -> None:
    url = "https://example.com/" + "a" * 100
    assert wrap(f"see {url} now", "", "") == ["see", url, "now"]


def test_wrap_keeps_a_code_span_whole() -> None:
    text = "x" * 60 + " `a long span here` tail"
    assert wrap(text, "", "") == ["x" * 60, "`a long span here` tail"]


def test_sections_become_plain_headings_with_blank_lines_between() -> None:
    body = "## Why\n\nBecause.\n\nTwice.\n\n## Risk\n\nNone here.\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nBecause.\n\nTwice.\n\nRisk\nNone here.\n"
    )


def test_comments_and_empty_sections_vanish() -> None:
    body = (
        "<!-- guidance\nacross lines -->\n\n## Why\n\n<!-- Required -->\n\n"
        "## Decisions\n\n<!-- Optional -->\n\n## Risk\n\nReal text. <!-- x -->\n"
    )
    assert message_of(body) == "fix: x (#7)\n\nRisk\nReal text.\n"


def test_preamble_text_comes_first() -> None:
    assert (
        message_of("Lead in.\n\n## Why\n\nw\n") == "fix: x (#7)\n\nLead in.\n\nWhy\nw\n"
    )


def test_prose_wraps_and_joins_soft_breaks() -> None:
    words = " ".join(["word"] * 40)
    out = message_of(f"## Why\n\n{words[:50]}\n{words[50:]}\n").splitlines()
    assert max(map(len, out)) <= WIDTH
    assert " ".join(out[3:]).split() == ["word"] * 40


def test_list_items_keep_markers_with_a_hanging_indent() -> None:
    items = "- " + "alpha " * 20 + "\n  more\n2. second\n  * nested\n"
    out = message_of(f"## Verification\n\n{items}").splitlines()
    assert out[3].startswith("- alpha")
    assert out[4].startswith("  alpha") or out[4].startswith("  more")
    assert any(line.startswith("2. second") for line in out)
    assert "  * nested" in out
    assert all(len(line) <= WIDTH for line in out)


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ("- a\n- b\n", "- a\n- b\n"),
        ("Intro\n\n- a\n- b\n\nAfter\n", "Intro\n\n- a\n- b\n\nAfter\n"),
        ("Para\n- a\n- b\n", "Para\n\n- a\n- b\n"),
        ("- a\n\n- b\n", "- a\n\n- b\n"),
        ("- a\n```\nx\n```\n- b\n", "- a\n\n    x\n\n- b\n"),
    ],
)
def test_a_tight_list_stays_one_block(body: str, want: str) -> None:
    assert body_text(body) == want


def test_fenced_code_is_indented_not_wrapped() -> None:
    long = "$ " + "x" * 100
    body = f"## Verification\n\nRan:\n\n```console\n{long}\n\nok\n```\n\nDone.\n"
    assert message_of(body) == (
        f"fix: x (#7)\n\nVerification\nRan:\n\n    {long}\n\n    ok\n\nDone.\n"
    )


def test_a_longer_fence_ignores_a_shorter_inner_one() -> None:
    body = "## Why\n\n````md\n```\ninner\n```\n````\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n    ```\n    inner\n    ```\n"


def test_other_fence_characters_do_not_close() -> None:
    body = "## Why\n\n~~~\n```\n~~~\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n    ```\n"


def test_a_table_keeps_its_rows_unwrapped() -> None:
    body = (
        "## Verification\n\nChecks:\n"
        "| Check | Result |\n|---|---|\n"
        f"| `uv run pytest` | {LONG_CELL} |\n  | b | **c** |  \n"
        "Then prose.\n\n| x |\n\n| y |\n"
    )
    assert message_of(body) == (
        "fix: x (#7)\n\nVerification\nChecks:\n\n"
        f"| Check | Result |\n|---|---|\n| `uv run pytest` | {LONG_CELL} |\n"
        "| b | **c** |\n\nThen prose.\n\n| x |\n\n| y |\n"
    )


def test_a_table_ends_a_list_and_a_fence_keeps_its_bars() -> None:
    body = "## Why\n\nLead.\n\n- a\n| x |\n|---|\n| z |\n- b\n\n```\n| y |\n```\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nLead.\n\n- a\n\n| x |\n|---|\n| z |\n\n- b\n\n    | y |\n"
    )


def test_a_delimiter_row_needs_no_outer_bars() -> None:
    body = "## Why\n\n| a | b |\n:--- | ---:\n| 1 | 2 |\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n| a | b |\n:--- | ---:\n| 1 | 2 |\n"


def test_a_table_may_end_on_its_delimiter_row() -> None:
    lines = [
        Line("| a |", "| a |", code=False),
        Line("|-|", "|-|", code=False),
    ]
    assert render(lines, {}, INLINE_CODE) == ["| a |\n|-|"]


def test_a_bar_line_without_a_delimiter_row_is_prose() -> None:
    body = f"## Why\n\n- a\n| {LONG_CELL}\n|\n| x |\n\n| y |\n|---|\nz\n| w |\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\n"
        "- a | the check ran again ran again ran again ran again ran again ran\n"
        "  again ran again ran again ran again ran again and passed | | x |\n\n"
        "| y |\n|---|\n\nz | w |\n"
    )


def test_links_become_numbered_references() -> None:
    body = (
        "## Why\n\nSee [the issue](https://x.test/1) and "
        "[the spec](https://x.test/2) and [again](https://x.test/1).\n"
        "Bare https://x.test/3 and <https://x.test/4> and "
        "[https://x.test/5](https://x.test/5).\n"
    )
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nSee the issue [1] and the spec [2] and again [1]. "
        "Bare https://x.test/3\nand https://x.test/4 and https://x.test/5.\n\n"
        "[1]: https://x.test/1\n[2]: https://x.test/2\n"
    )


def test_emphasis_goes_and_inline_code_stays() -> None:
    body = "## Why\n\nA **strong** and *soft* word, `*raw* [a](b)` and 2 * 3 * 4.\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nA strong and soft word, `*raw* [a](b)` and 2 * 3 * 4.\n"
    )


def test_a_nested_heading_is_kept_as_text() -> None:
    assert message_of("## Why\n\n### Sub\n\nx\n") == "fix: x (#7)\n\nWhy\nSub\n\nx\n"


def test_an_empty_body_is_a_subject_only() -> None:
    assert message_of("") == "fix: x (#7)\n"
    assert message_of("<!-- only -->\n") == "fix: x (#7)\n"


@given(text=TEXT)
@example(
    text="|0 00000000000 00000000000 00000000000 000000000000000 0000000000000000 0"
)
def test_rendered_prose_is_within_the_limit(text: str) -> None:
    text = text.replace("\\", "").replace("*", "").replace("_", "").replace("[", "")
    out = render([Line(text, text, code=False)], {}, INLINE_CODE)
    for line in "\n".join(out).splitlines():
        assert len(line) <= WIDTH or len(TOKEN.findall(line)) == 1


def test_one_line_paragraphs_still_wrap_at_72() -> None:
    sentence = " ".join(["word"] * 60)
    out = message_of(f"## Why\n\n{sentence}\n\n- {sentence}\n").splitlines()
    assert len(out) > 8
    assert all(len(line) <= WIDTH for line in out)
    assert out[3].startswith("word word")
    assert any(line.startswith("  word") for line in out)


def test_an_empty_fence_does_not_make_a_preamble() -> None:
    assert body_text("```\n```\n") == ""
