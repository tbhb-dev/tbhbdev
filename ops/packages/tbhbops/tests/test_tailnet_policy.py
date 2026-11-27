import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tbhbops.tailnet_policy import HEADER, check, standardize

@pytest.mark.parametrize(
    ("hujson", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('{"a": 1, // note\n}', {"a": 1}),
        ('{"a": [1, 2,], /* x\n y */ "b": 2,}', {"a": [1, 2], "b": 2}),
        ('{"a": [1 ,\n\t]}', {"a": [1]}),
        (
            '{"url": "https://x//y", "c": "/* no */"}',
            {"url": "https://x//y", "c": "/* no */"},
        ),
        ('{"q": "a\\",]"}', {"q": 'a",]'}),
        ('{"t": ",]", "u": ",}"}', {"t": ",]", "u": ",}"}),
    ],
)
def test_standardize_keeps_the_data(hujson: str, expected: object) -> None:
    assert json.loads(standardize(hujson)) == expected


def test_a_comment_becomes_a_space() -> None:
    assert standardize("1/* x */2") == "1 2"
    assert standardize("1// x\n2") == "1 \n2"


def test_a_comma_goes_only_before_a_closer() -> None:
    assert standardize("[1, 2]") == "[1, 2]"
    assert standardize("[1, ]") == "[1 ]"
    assert standardize("{1, }") == "{1 }"


@pytest.mark.parametrize(
    "bad",
    [
        '{"a": 1,,}',
        '{"a": 1 /* open',
        '{"a": "open\n"}',
        "{a: 1}",
        "[1, 2",
    ],
)
def test_check_rejects_what_hujson_rejects(bad: str) -> None:
    problems = check(f"{HEADER}\n{bad}")
    assert len(problems) == 1
    assert problems[0].startswith("not valid HuJSON: ")


def test_check_wants_the_header_first() -> None:
    assert check("{}") == [f"the first line must be exactly: {HEADER}"]
    assert check(f"\n{HEADER}\n{{}}") == [f"the first line must be exactly: {HEADER}"]
    assert check(f"{HEADER} \n{{}}") == [f"the first line must be exactly: {HEADER}"]
    assert check(f"{HEADER}\n{{}}") == []


def test_check_reports_both_problems() -> None:
    assert len(check("{")) == 2


@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(),
        lambda inner: st.lists(inner) | st.dictionaries(st.text(), inner),
    )
)
def test_plain_json_passes_through(value: object) -> None:
    for text in (json.dumps(value), json.dumps(value, indent=1)):
        assert json.loads(standardize(text)) == value
