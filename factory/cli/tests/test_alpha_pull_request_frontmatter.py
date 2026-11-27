# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Test pull request frontmatter with in-memory drafts and schemas."""

import pytest
from jsonschema import Draft202012Validator

from tbhbdev.alpha import pull_request_frontmatter as frontmatter

SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "base": {"type": "string", "default": "main"},
        "draft": {"type": "boolean", "default": False},
        "labels": {"type": "array", "uniqueItems": True},
    },
}
VALIDATOR = Draft202012Validator(SCHEMA)


def test_parse_frontmatter_splits_block_and_body() -> None:
    assert frontmatter.parse_frontmatter("---\nbase: dev\n---\nbody\n") == (
        {"base": "dev"},
        "body\n",
    )
    assert frontmatter.parse_frontmatter("---\nbase: dev\n---  \nbody") == (
        {"base": "dev"},
        "body",
    )
    assert frontmatter.parse_frontmatter("---\n---\nbody") == (None, "body")
    assert frontmatter.parse_frontmatter("plain\n") == (None, "plain\n")


def test_parse_frontmatter_keeps_dates_as_text() -> None:
    data, _ = frontmatter.parse_frontmatter("---\nmilestone: 2024-01-02\n---\n")
    assert data == {"milestone": "2024-01-02"}


def test_parse_frontmatter_errors() -> None:
    with pytest.raises(
        frontmatter.FrontmatterError, match=r"^unterminated frontmatter$"
    ):
        frontmatter.parse_frontmatter("---\nbase: x\n")
    with pytest.raises(frontmatter.FrontmatterError) as info:
        frontmatter.parse_frontmatter("---\nbase: [\n---\n")
    message = str(info.value)
    assert message.startswith("invalid YAML: ")
    assert "\n" not in message


def test_frontmatter_refuses_a_repeated_key() -> None:
    with pytest.raises(frontmatter.FrontmatterError, match="duplicate key 'base'"):
        frontmatter.parse_frontmatter("---\nbase: main\nbase: dev\n---\n")


def test_schema_default() -> None:
    assert frontmatter.schema_default(SCHEMA, "base") == "main"
    assert frontmatter.schema_default(SCHEMA, "draft") is False


def test_frontmatter_unknown_key_is_line_one() -> None:
    text = "---\ndraft: true\ntitle: x\n---\n"
    data, _ = frontmatter.parse_frontmatter(text)
    [(line, message)] = frontmatter.meta_problems(data, 4, text, VALIDATOR)
    assert line == 1
    assert "title" in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\nbase: main\ndraft: maybe\nlabels: [a]\n---\n", [(3, "$.draft")]),
        ("---\nlabels: [a, a]\ndraft: 1\n---\n", [(3, "$.draft"), (2, "$.labels")]),
        ("---\n- a\n---\n", [(1, "$")]),
        # The fallback does not pick up a matching key in the body.
        ('---\n"base": 3\n---\nbase: x\n', [(1, "$.base")]),
        ("---\n---\n", []),
    ],
)
def test_meta_problems(text: str, expected: list[tuple[int, str]]) -> None:
    data, body = frontmatter.parse_frontmatter(text)
    offset = text.count("\n") - body.count("\n")
    problems = frontmatter.meta_problems(data, offset, text, VALIDATOR)
    assert [(line, msg.split(": ", 1)[0]) for line, msg in problems] == [
        (line, f"frontmatter {key}") for line, key in expected
    ]
