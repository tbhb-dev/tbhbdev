# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity frontmatter tests."""

from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from integration.alpha.identity.identity_test_support import (
    AT1,
    AT2,
    CHILD,
    HEAD,
    OTHER,
    Entry,
    H,
    document,
    entry,
    front_of,
    listing,
)

from tbhbdev.alpha.identity import frontmatter
from tbhbdev.alpha.identity import trailers as identity_trailers

VERIFIED = 'verified:\n  by: human:tony\n  at: "2026-01-01T00:00:00Z"\n'

SESSION = H + "1" * 16


def test_valid_entry() -> None:
    assert frontmatter.valid_entry(entry())
    full = entry(
        effort="high",
        surface="cli",
        session=SESSION,
        subagent=CHILD,
        role="assisted",
        harness="codex",
        model="openai:gpt-5.6-sol",
    )
    assert frontmatter.valid_entry(full)


@pytest.mark.parametrize(
    "bad",
    [
        {"role": "author"},
        {"harness": "gemini-cli"},
        {"harness": "claude-code:"},
        {"harness": "claude-code:2.1 beta"},
        {"model": "claude-opus-5-5"},
        {"model": "azure:gpt"},
        {"model": "anthropic:"},
        {"effort": "High"},
        {"surface": "a;b"},
        {"surface": "a,b"},
        {"surface": "café"},
        {"surface": "a b"},
        {"surface": ""},
        {"session": "sha-256-64:ABCDEF0123456789"},
        {"session": "sha-256-64:abc"},
        {"subagent": "raw-id"},
        {"at": "2026-11-02 10:00:00"},
        {"at": "2026-11-02T10:00:00+00:00"},
        {"unknown": "x"},
    ],
)
def test_valid_entry_rejects(bad: Entry) -> None:
    assert not frontmatter.valid_entry(entry(**bad))


@pytest.mark.parametrize("missing", ["role", "harness", "model", "at"])
def test_valid_entry_requires(missing: str) -> None:
    incomplete = entry()
    del incomplete[missing]
    assert not frontmatter.valid_entry(incomplete)


def test_contributor_key_ignores_version_role_and_time() -> None:
    first = entry(session=SESSION, subagent=CHILD)
    other = entry(
        session=SESSION,
        subagent=CHILD,
        harness="claude-code",
        role="assisted",
        at=AT2,
        effort="low",
        surface="cli",
    )
    assert frontmatter.contributor_key(first) == frontmatter.contributor_key(other)
    assert frontmatter.contributor_key(first) == (
        "claude-code",
        "anthropic:claude-opus-5-5",
        SESSION,
        CHILD,
    )
    assert frontmatter.contributor_key(entry()) == (
        "claude-code",
        "anthropic:claude-opus-5-5",
        None,
        None,
    )
    for change in (
        {"harness": "codex"},
        {"model": "anthropic:other"},
        {"session": OTHER},
        {"subagent": OTHER},
    ):
        assert frontmatter.contributor_key(
            {**first, **change}
        ) != frontmatter.contributor_key(first)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("claude-code:2.1.316", "claude-code:2.1.316"),
        ("gcp.gemini:gemini-3.8-flash", "gcp.gemini:gemini-3.8-flash"),
        (SESSION, SESSION),
        ("a/b+c_d", "a/b+c_d"),
        ("a:", '"a:"'),
        ("n", '"n"'),
        ("no", '"no"'),
        ("Yes", '"Yes"'),
        ("NULL", '"NULL"'),
        ("true", '"true"'),
        ("null", '"null"'),
        ("off", '"off"'),
        ("123", '"123"'),
        ("-x", '"-x"'),
        ("*x", '"*x"'),
        ('a"b', '"a\\"b"'),
        ("a\\b", '"a\\\\b"'),
        ("a#b", '"a#b"'),
        (AT1, f'"{AT1}"'),
    ],
)
def test_yaml_scalar(value: str, expected: str) -> None:
    assert frontmatter.yaml_scalar(value) == expected


def test_yaml_scalar_words_are_matched_exactly() -> None:
    assert frontmatter.yaml_scalar("nope") == "nope"
    assert frontmatter.yaml_scalar("yes") == '"yes"'
    assert frontmatter.yaml_scalar("false") == '"false"'
    assert frontmatter.yaml_scalar("on") == '"on"'
    assert frontmatter.yaml_scalar("y") == '"y"'
    assert frontmatter.yaml_scalar("ab") == "ab"
    assert frontmatter.yaml_scalar("a") == "a"
    assert frontmatter.yaml_scalar("a-") == "a-"


def test_render_entry_orders_the_fields_canonically() -> None:
    full = entry(
        effort="high", surface="cli", session=SESSION, subagent=CHILD, role="assisted"
    )
    shuffled = dict(reversed(list(full.items())))
    expected = [
        "- role: assisted",
        "  harness: claude-code:2.1.316",
        "  model: anthropic:claude-opus-5-5",
        "  effort: high",
        "  surface: cli",
        f"  session: {SESSION}",
        f"  subagent: {CHILD}",
        f'  at: "{AT1}"',
    ]
    assert frontmatter.render_entry(shuffled, 0) == expected
    assert frontmatter.render_entry(full, 3) == [
        "   - role: assisted",
        *[f"     {line[2:]}" for line in expected[1:]],
    ]
    assert frontmatter.render_entry({"model": "m"}, 1) == [" - model: m"]
    assert frontmatter.render_entry({}, 1) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("1.2.3", "1.2.3"),
        ("a:b", "a:b"),
        ("plain  ", "plain"),
        ('"quoted value"', "quoted value"),
        ('""', ""),
        ('"a\\"b\\\\c"', 'a"b\\c'),
        ("'single'", "single"),
        ("''", ""),
        ("'a\"b'", 'a"b'),
        ('"a"  ', "a"),
    ],
)
def test_unquote(raw: str, expected: str) -> None:
    assert frontmatter.unquote(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "  ",
        "a #comment",
        "a #",
        "#a",
        "[a]",
        "{a: b}",
        "|",
        ">",
        "&anchor",
        "*alias",
        "!tag",
        '"unterminated',
        '"a" "b"',
        '"a\\nb"',
        "'a'b'",
        '"a" # c',
        "-x",
        "a b",
    ],
)
def test_unquote_rejects(raw: str) -> None:
    assert frontmatter.unquote(raw) is None


def test_parse_entries_reads_block_lists() -> None:
    lines = [
        "  - role: generated",
        "    harness: 'claude-code:1'",
        '    model: "anthropic:m"',
        f"    at: {AT1}",
        "  - role: assisted",
        "    model: x",
    ]
    parsed = frontmatter.parse_entries(lines)
    assert parsed is not None
    assert [(p.first, p.last, p.indent) for p in parsed] == [(0, 4, 2), (4, 6, 2)]
    assert parsed[0].fields == {
        "role": "generated",
        "harness": "claude-code:1",
        "model": "anthropic:m",
        "at": AT1,
    }
    assert parsed[1].fields == {"role": "assisted", "model": "x"}
    assert frontmatter.parse_entries([]) == []
    top = frontmatter.parse_entries(["- role: generated", "  at: x"])
    assert top is not None
    assert (top[0].indent, top[0].last) == (0, 2)
    deep = frontmatter.parse_entries(["    - role: generated", "      at: x"])
    assert deep is not None
    assert deep[0].indent == 4


@pytest.mark.parametrize(
    "lines",
    [
        ["  role: generated"],
        ["  - role: generated", "   harness: x"],
        ["  - role: generated", "     harness: x"],
        ["  - role: generated", "  harness: x"],
        ["  - role: generated", "    role: assisted"],
        ["  - role: generated", "    # comment"],
        ["  - role: generated", ""],
        ["  - role:"],
        ["  - role: [a]"],
        ["  - Role: x"],
        ["  - role: generated", "    - nested"],
        ["  - role: generated", "    harness: x #c"],
        ["# comment", "  - role: generated"],
    ],
)
def test_parse_entries_rejects(lines: list[str]) -> None:
    assert frontmatter.parse_entries(lines) is None


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # Newer sighting: at, effort, surface and a harness version move forward.
        (
            entry(effort="low", surface="cli"),
            entry(at=AT2, effort="high", surface="vscode", harness="claude-code:2.2"),
            entry(at=AT2, effort="high", surface="vscode", harness="claude-code:2.2"),
        ),
        # Same time counts as newer.
        (entry(effort="low"), entry(effort="high"), entry(effort="high")),
        # A sighting without those fields keeps what is known.
        (
            entry(effort="low", surface="cli"),
            entry(at=AT2, harness="claude-code"),
            entry(at=AT2, effort="low", surface="cli"),
        ),
        # Older sighting: only fills what is missing.
        (
            entry(at=AT2, effort="low"),
            entry(effort="high", surface="cli"),
            entry(at=AT2, effort="low", surface="cli"),
        ),
        (
            entry(at=AT2, harness="claude-code"),
            entry(harness="claude-code:1.0"),
            entry(at=AT2, harness="claude-code:1.0"),
        ),
        (
            entry(at=AT2, harness="claude-code:2.0"),
            entry(harness="claude-code:1.0"),
            entry(at=AT2, harness="claude-code:2.0"),
        ),
        (
            entry(harness="claude-code:1.0"),
            entry(at=AT2, harness="claude-code"),
            entry(at=AT2, harness="claude-code:1.0"),
        ),
        # Generated beats assisted, and never the reverse.
        (entry(role="assisted"), entry(role="generated"), entry()),
        (
            entry(role="assisted"),
            entry(role="assisted", at=AT2),
            entry(role="assisted", at=AT2),
        ),
        (entry(), entry(role="assisted", at=AT2, effort="x"), entry()),
        (entry(role="generated"), entry(role="generated", at=AT2), entry(at=AT2)),
    ],
)
def test_merge_entry(old: Entry, new: Entry, expected: Entry) -> None:
    assert frontmatter.merge_entry(old, new) == expected


def test_merge_entry_does_not_mutate_its_arguments() -> None:
    old, new = entry(effort="low"), entry(at=AT2, effort="high")
    snapshot = (dict(old), dict(new))
    frontmatter.merge_entry(old, new)
    assert (old, new) == snapshot


def test_stamp_text_adds_the_key_before_the_closing_fence() -> None:
    text = document()
    out = frontmatter.stamp_text(text, [entry(effort="high")])
    assert out == document(
        "generated:",
        "  - role: generated",
        "    harness: claude-code:2.1.316",
        "    model: anthropic:claude-opus-5-5",
        "    effort: high",
        f'    at: "{AT1}"',
    )
    assert front_of(out)["generated"] == [
        {
            "role": "generated",
            "harness": "claude-code:2.1.316",
            "model": "anthropic:claude-opus-5-5",
            "effort": "high",
            "at": AT1,
        }
    ]


def test_stamp_text_keeps_everything_around_the_block() -> None:
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    text = document(
        "generated:",
        *listing(entry()),
        "verified:",
        "  by: human:tony",
        f'  at: "{AT1}"',
        body="body\n\n# T\n---\nx\r\n",
    )
    out = frontmatter.stamp_text(text, [other])
    expected = document(
        "generated:",
        *listing(other),
        "verified:",
        "  by: human:tony",
        f'  at: "{AT1}"',
        body="body\n\n# T\n---\nx\r\n",
    )
    assert out == expected


def test_stamp_text_replaces_in_the_existing_indent() -> None:
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    text = document("generated:", *listing(entry(), indent=0))
    out = frontmatter.stamp_text(text, [other])
    assert out == document("generated:", *listing(other, indent=0))
    wide = document("generated:", *listing(entry(), indent=4))
    assert frontmatter.stamp_text(wide, [other]) == document(
        "generated:", *listing(other, indent=4)
    )


def test_stamp_text_replaces_rather_than_merges() -> None:
    before = document("generated:", *listing(entry(role="assisted", effort="low")))
    out = frontmatter.stamp_text(before, [entry(at=AT2, surface="cli")])
    assert out == document("generated:", *listing(entry(at=AT2, surface="cli")))


def test_stamp_text_keeps_only_the_latest_generation() -> None:
    first = entry(model="openai:gpt-5.6-sol", harness="codex", effort="low")
    second = entry(effort="low")
    third = entry(model="anthropic:other")
    before = document("generated:", *listing(first, second, third))
    latest = [entry(at=AT2, effort="high"), entry(at=AT2, model="anthropic:other")]
    out = frontmatter.stamp_text(before, latest)
    assert out == document("generated:", *listing(*latest))


def test_stamp_text_does_not_grow_across_sessions() -> None:
    text = document()
    for n in range(5):
        session = f"sha-256-64:{n:016x}"
        text = frontmatter.stamp_text(text, [entry(session=session)])
    assert front_of(text)["generated"] == [entry(session=f"sha-256-64:{4:016x}")]


def test_stamp_text_leaves_the_same_generation_byte_for_byte() -> None:
    odd = [
        "generated:",
        '- role: "generated"',
        "  harness: 'claude-code:2.1.316'",
        "  model: anthropic:claude-opus-5-5",
        f"  at: {AT1}",
    ]
    text = document(*odd)
    assert frontmatter.stamp_text(text, [entry()]) == text
    assert frontmatter.stamp_text(text, [entry(role="assisted", at=AT2)]) != text


def test_stamp_text_adds_a_changed_field_in_canonical_form() -> None:
    odd = [
        "generated:",
        '- role: "generated"',
        "  harness: 'claude-code:2.1.316'",
        "  model: anthropic:claude-opus-5-5",
        f"  at: {AT1}",
    ]
    out = frontmatter.stamp_text(document(*odd), [entry(at=AT2, effort="high")])
    assert out == document(
        "generated:", *listing(entry(at=AT2, effort="high"), indent=0)
    )


def test_stamp_text_empty_forms() -> None:
    expected = document("generated:", *listing(entry()))
    assert frontmatter.stamp_text(document("generated: []"), [entry()]) == expected
    assert frontmatter.stamp_text(document("generated:"), [entry()]) == expected
    assert frontmatter.stamp_text(document("generated: [] "), [entry()]) == expected


@pytest.mark.parametrize(
    "lines",
    [
        ["generated: ~"],
        ["generated: null"],
        ["generated: [{role: generated}]"],
        ["generated: &a"],
        ["generated:", "  # note", *listing(entry())],
        ["generated:", *listing(entry()), "  # note"],
        ["generated:", "  - role: generated", "    note: x y"],
        ["generated:", "  - 5"],
    ],
)
def test_stamp_text_leaves_unsupported_generated_values(lines: list[str]) -> None:
    text = document(*lines)
    assert (
        frontmatter.stamp_text(
            text, [entry(model="openai:gpt-5.6-sol", harness="codex")]
        )
        == text
    )


def test_stamp_text_leaves_an_unsupported_list_with_several_entries() -> None:
    text = document("generated:", *listing(entry()), "  # note")
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    assert frontmatter.stamp_text(text, [entry(at=AT2), other]) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no frontmatter\n",
        "\n---\ntype: x\n---\n",
        "---\ntype: x\n",
        "---\r\ntype: x\r\n---\r\n",
        " ---\ntype: x\n---\n",
        "---x\ntype: x\n---\n",
        "----\ntype: x\n----\n",
    ],
)
def test_stamp_text_needs_lf_frontmatter(text: str) -> None:
    assert frontmatter.stamp_text(text, [entry()]) == text


def test_stamp_text_accepts_a_closing_fence_with_trailing_blanks() -> None:
    out = frontmatter.stamp_text("---\ntype: x\n---  \t\nbody\n", [entry()])
    assert (
        out
        == "---\ntype: x\n"
        + "\n".join(["generated:", *listing(entry())])
        + "\n---  \t\nbody\n"
    )
    assert "---\n---\n" not in out
    assert frontmatter.stamp_text("---\n---\n", [entry()]) == "\n".join(
        ["---", "generated:", *listing(entry()), "---", ""]
    )
    assert (
        frontmatter.stamp_text("---\ntype: x\n--- x\n---\n", [entry()]).count(
            "generated:"
        )
        == 1
    )


def test_stamp_text_ignores_look_alike_keys() -> None:
    text = document("generated_by: x", " generated:", "xgenerated:")
    out = frontmatter.stamp_text(text, [entry()])
    assert out.count("\ngenerated:\n") == 1
    assert out.endswith("\n".join(listing(entry())) + "\n---\n# T\n")
    text = document("generated:", *listing(entry()), "generated:")
    assert frontmatter.stamp_text(text, [entry(at=AT2)]) == document(
        "generated:", *listing(entry(at=AT2)), "generated:"
    )


def test_stamp_text_only_looks_for_the_key_in_the_frontmatter() -> None:
    text = document(body="generated:\n  - role: generated\n")
    out = frontmatter.stamp_text(text, [entry()])
    assert out.startswith(
        "---\n" + HEAD + "generated:\n  - role: generated\n    harness"
    )


def test_stamp_text_drops_invalid_entries_and_stamps_nothing_for_none() -> None:
    text = document()
    assert frontmatter.stamp_text(text, []) == text
    assert frontmatter.stamp_text(text, [entry(model="nope")]) == text
    assert frontmatter.stamp_text(text, [{"role": "generated"}]) == text
    out = frontmatter.stamp_text(text, [entry(model="nope"), entry()])
    assert out == document("generated:", *listing(entry()))


def test_stamp_text_puts_generated_before_assisted() -> None:
    assisted = entry(role="assisted", model="openai:gpt-5.6-sol", harness="codex")
    out = frontmatter.stamp_text(document(), [assisted, entry()])
    assert out == document("generated:", *listing(entry(), assisted))


def test_stamp_text_handles_repeated_contributors_in_one_call() -> None:
    out = frontmatter.stamp_text(
        document(), [entry(effort="low"), entry(at=AT2, effort="high")]
    )
    assert out == document("generated:", *listing(entry(at=AT2, effort="high")))


def test_stamp_text_is_idempotent() -> None:
    entries = [
        entry(effort="high", surface="cli"),
        entry(role="assisted", harness="codex", model="openai:gpt-5.6-sol"),
    ]
    once = frontmatter.stamp_text(document(), entries)
    assert frontmatter.stamp_text(once, entries) == once
    assert once != document()


def test_stamp_text_accepts_any_iterable() -> None:
    out = frontmatter.stamp_text(document(), iter([entry()]))
    assert out == document("generated:", *listing(entry()))


def hashed() -> st.SearchStrategy[str]:
    return st.from_regex(frontmatter.DOC_PATTERNS["session"], fullmatch=True)


def entries() -> st.SearchStrategy[Entry]:
    when = st.datetimes(
        min_value=__import__("datetime").datetime(2000, 1, 1),
        max_value=__import__("datetime").datetime(2099, 12, 31),
    ).map(lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return st.fixed_dictionaries(
        {
            "role": st.sampled_from(["generated", "assisted"]),
            "harness": st.from_regex(
                frontmatter.DOC_PATTERNS["harness"], fullmatch=True
            ),
            "model": st.from_regex(frontmatter.DOC_PATTERNS["model"], fullmatch=True),
            "at": when,
        },
        optional={
            "effort": st.from_regex(frontmatter.DOC_PATTERNS["effort"], fullmatch=True),
            "surface": st.from_regex(
                frontmatter.DOC_PATTERNS["surface"], fullmatch=True
            ),
            "session": hashed(),
            "subagent": hashed(),
        },
    )


@settings(deadline=None)
@given(
    first=st.lists(entries(), max_size=4),
    second=st.lists(entries(), max_size=4),
    tail=st.sampled_from(["", VERIFIED]),
    body=st.binary(max_size=40),
)
def test_stamp_text_preserves_bytes_and_validates(
    first: list[Entry], second: list[Entry], tail: str, body: bytes
) -> None:
    text = "---\n" + HEAD + tail + "---\n" + body.decode(errors="surrogateescape")
    once = frontmatter.stamp_text(text, first)
    twice = frontmatter.stamp_text(once, second)
    for out in (once, twice):
        assert out.startswith("---\n" + HEAD + tail)
        assert out.endswith("---\n" + body.decode(errors="surrogateescape"))
        assert out.encode(errors="surrogateescape").endswith(b"---\n" + body)
        # Each stamp replaces the list; an empty one leaves it as it was.
        latest = first if out is once or not second else second
        keys = {frontmatter.contributor_key(e) for e in latest}
        front = front_of(out)
        assert len(cast("list[object]", front.get("generated", []))) == len(keys)
    assert frontmatter.stamp_text(twice, second) == twice
    assert frontmatter.stamp_text(once, first) == once
    for out in (once, twice):
        block = (
            out.split("generated:\n")[-1].split("---\n")[0]
            if "generated:" in out
            else ""
        )
        if block and not tail:
            parsed = frontmatter.parse_entries(block.splitlines())
            assert parsed is not None
            assert all(frontmatter.valid_entry(p.fields) for p in parsed)


def test_utc_formats_whole_seconds() -> None:
    assert identity_trailers.utc(0) == "1970-01-01T00:00:00Z"
    assert identity_trailers.utc(1_800_000_000.9) == "2027-01-15T08:00:00Z"


def test_parse_entries_a_one_line_item_ends_on_the_next_line() -> None:
    (only,) = frontmatter.parse_entries(["  - role: generated"]) or []
    assert (only.first, only.last) == (0, 1)
    first, second = (
        frontmatter.parse_entries(["- role: generated", "- role: assisted"]) or []
    )
    assert (first.first, first.last, second.first, second.last) == (0, 1, 1, 2)


def test_stamp_text_closing_fence_ends_at_a_space_or_tab_only() -> None:
    for tail in ("\r", "X", "\f", "\v"):
        text = f"---\ntype: x\n---{tail}\nbody\n---\n"
        expected = f"---\ntype: x\n---{tail}\nbody\n" + "\n".join(
            ["generated:", *listing(entry())]
        )
        assert frontmatter.stamp_text(text, [entry()]) == expected + "\n---\n"


def test_stamp_text_finds_generated_as_the_first_key() -> None:
    text = "---\n" + "\n".join(["generated:", *listing(entry())]) + "\ntype: x\n---\n"
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    out = frontmatter.stamp_text(text, [other])
    assert (
        out == "---\n" + "\n".join(["generated:", *listing(other)]) + "\ntype: x\n---\n"
    )
