# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity trailers."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from unit.alpha.identity.identity_examples import (
    ASSIST_OTHER,
    CHILD,
    EARLY,
    GEN_SUB,
    LATE,
    LAUNCH,
    OTHER,
    ROOT,
    H,
    line_of,
)

from tbhbdev.alpha.identity import models, trailers
from tbhbdev.alpha.identity.commit import grammar as commit_grammar
from tbhbdev.alpha.identity.models import EFFORTS

harnesses = st.sampled_from(sorted(models.SLUGS.values())).flatmap(
    lambda slug: st.one_of(
        st.just(slug),
        st.from_regex(r"[0-9]\.[0-9]{1,2}", fullmatch=True).map(
            lambda ver: f"{slug}:{ver}"
        ),
    )
)


hashes = st.from_regex(r"sha-256-64:[0-9a-f]{16}", fullmatch=True)


attr_sets = st.fixed_dictionaries(
    {
        "harness": harnesses,
        "model": st.from_regex(r"[a-z0-9][a-z0-9.:_-]{0,20}", fullmatch=True),
    },
    optional={
        "effort": st.sampled_from(sorted(EFFORTS)),
        "session": hashes,
        "subagent": hashes,
        "surface": st.from_regex(r"[A-Za-z0-9_-]{1,10}", fullmatch=True),
        "variant": st.sampled_from(["a", "b"]),
    },
)


@settings(database=None)
@given(attr_sets)
def test_grammar_round_trip(attrs: dict[str, str]) -> None:
    value = trailers.format_value(attrs)
    assert value.startswith("LLM ")
    assert commit_grammar.check_value(value) is None
    pairs = [pair.split("=", 1) for pair in value.removeprefix("LLM ").split(";")]
    assert dict(pairs) == attrs
    keys = [key for key, _ in pairs]
    assert keys == [key for key in trailers.ATTR_ORDER if key in attrs]


def test_format_order_and_omission() -> None:
    attrs = {
        "surface": "cli",
        "subagent": H + "b" * 16,
        "model": "anthropic:m",
        "harness": "claude-code:2.1.316",
        "effort": "high",
        "session": ROOT,
    }
    assert trailers.format_value(attrs) == (
        "LLM effort=high;harness=claude-code:2.1.316;model=anthropic:m;"
        f"session={ROOT};subagent={H}{'b' * 16};surface=cli"
    )
    assert (
        trailers.format_value({"harness": "antigravity"}) == "LLM harness=antigravity"
    )
    assert (
        trailers.format_value({"harness": "codex", "model": ""}) == "LLM harness=codex"
    )
    assert sorted(trailers.ATTR_ORDER) == trailers.ATTR_ORDER
    assert {"launch", "started_at", "variant"} <= set(trailers.ATTR_ORDER)
    assert len(trailers.ATTR_ORDER) == 9


GEN_ROOT = ("Generated-by", f"LLM harness=claude-code;session={ROOT}")


ASSIST_ROOT = ("Assisted-by", f"LLM harness=claude-code;session={ROOT}")


def test_llm_trailers_reads_only_llm_trailer_lines() -> None:
    text = (
        "s\n\nGenerated-by: robot\nNote: Generated-by: LLM x=y\n"
        f"{GEN_ROOT[0]}: {GEN_ROOT[1]}\nAssisted-by: LLMX a=b\n"
        f"{ASSIST_OTHER[0]}: {ASSIST_OTHER[1]}\n"
    )
    assert trailers.llm_trailers(text) == [GEN_ROOT, ASSIST_OTHER]


@pytest.mark.parametrize("gap", ["", "  ", "\t"])
def test_trailer_lines_read_any_gap_after_the_colon(gap: str) -> None:
    text = f"s\n\nGenerated-by:{gap}{GEN_ROOT[1]}\n"
    assert trailers.llm_trailers(text) == [GEN_ROOT]
    assert trailers.LLM_LINES.sub("", text) == "s\n\n"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"LLM harness=x;session={ROOT};subagent={CHILD}", (ROOT, CHILD)),
        (f"LLM session={ROOT}", (ROOT, None)),
        (f"LLM xsession={ROOT};model=a:subagent=b", (None, None)),
        ("LLM", (None, None)),
    ],
)
def test_trailer_agent(value: str, expected: tuple[str | None, str | None]) -> None:
    assert trailers.trailer_agent(value) == expected


def test_merge_trailers_dedupes_orders_and_lets_generated_win() -> None:
    found = [ASSIST_ROOT, ASSIST_OTHER, GEN_SUB, GEN_ROOT, GEN_SUB, ASSIST_OTHER]
    assert trailers.merge_trailers(found) == [GEN_SUB, GEN_ROOT, ASSIST_OTHER]


def test_utc_formats_whole_seconds() -> None:
    assert trailers.utc(0) == "1970-01-01T00:00:00Z"
    assert trailers.utc(1_800_000_000.9) == "2027-01-15T08:00:00Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (EARLY, EARLY),
        ("2026-02-30T00:00:00Z", None),
        ("2026-11-14T24:00:00Z", None),
        ("2026-11-14t00:02:41z", None),
        ("\u0662\u0660\u0662\u0666-11-14T00:02:41Z", None),
        ("2026-11-14T00:02:41", None),
        ("2026-11-14T00:02:41+00:00", None),
        ("2026-11-14T00:02:41.5Z", None),
        (" " + EARLY, None),
        (EARLY + "Z", None),
        (None, None),
        (5, None),
    ],
)
def test_start_ok(value: object, expected: str | None) -> None:
    assert trailers.start_ok(value) == expected


def test_without_start_and_trailer_start() -> None:
    value = f"LLM harness=h;model=m;session={ROOT};started_at={EARLY};subagent={CHILD}"
    assert (
        trailers.without_start(value)
        == f"LLM harness=h;model=m;session={ROOT};subagent={CHILD}"
    )
    assert (
        trailers.without_start(f"LLM x_started_at={EARLY}")
        == f"LLM x_started_at={EARLY}"
    )
    assert trailers.trailer_start(value) == EARLY
    assert trailers.trailer_start(f"LLM x_started_at={EARLY}") == ""
    assert trailers.trailer_start("LLM harness=h") == ""
    assert trailers.trailer_start("LLM started_at=0") == ""
    assert trailers.trailer_start("LLM started_at=2026-02-30T00:00:00Z") == ""


def test_merge_trailers_keeps_the_earliest_start_per_identity() -> None:
    gen, assist = "Generated-by", "Assisted-by"
    found = [
        (gen, line_of()),
        (gen, line_of(LATE)),
        (gen, line_of(EARLY, "effort=low;")),
        (gen, line_of(EARLY)),
        (gen, line_of(LATE, who=OTHER)),
        (gen, line_of(who=CHILD)),
        (gen, line_of(extra="effort=low;", who=CHILD)),
        (assist, line_of(LATE, who=H + "e" * 16)),
        (assist, line_of(EARLY, who=H + "e" * 16)),
    ]
    assert trailers.merge_trailers(found) == [
        (gen, line_of(EARLY)),
        (gen, line_of(EARLY, "effort=low;")),
        (gen, line_of(LATE, who=OTHER)),
        (gen, line_of(who=CHILD)),
        (gen, line_of(extra="effort=low;", who=CHILD)),
        (assist, line_of(EARLY, who=H + "e" * 16)),
    ]
    # The key is part of the identity, and each attribute of it counts.
    apart = [
        (gen, f"LLM harness=a;model=m;session={ROOT};started_at={LATE}"),
        (gen, f"LLM harness=b;model=m;session={ROOT};started_at={EARLY}"),
        (gen, f"LLM harness=a;model=n;session={ROOT};started_at={EARLY}"),
        (
            gen,
            f"LLM harness=a;model=m;session={ROOT};started_at={EARLY};subagent={CHILD}",
        ),
    ]
    assert trailers.earliest_starts(apart) == apart
    # A line that differs in any other attribute never takes another's place,
    # so a hand-written earlier time cannot drop the hook's launch or effort.
    real = (gen, line_of(LATE, f"launch={LAUNCH};"))
    forged = (gen, line_of(EARLY))
    assert trailers.merge_trailers([real, forged]) == [real, forged]
    mixed = [(assist, line_of(EARLY)), (gen, line_of(LATE, who=OTHER))]
    assert trailers.earliest_starts(mixed) == mixed


def test_identity_key_ignores_effort_surface_and_version() -> None:
    one = "LLM effort=low;harness=claude-code:1.0;model=a:m;surface=cli"
    two = "LLM effort=high;harness=claude-code:2.0;model=a:m"
    assert (
        trailers.identity_key(one)
        == trailers.identity_key(two)
        == ("claude-code", "a:m", "", "")
    )
    full = f"LLM harness=codex;model=a:m;session={ROOT};subagent={CHILD}"
    assert trailers.identity_key(full) == ("codex", "a:m", ROOT, CHILD)
    assert trailers.identity_key("LLM harness=codex") == ("codex", "", "", "")


def test_identity_key_splits_on_the_first_equals() -> None:
    value = "LLM harness=claude-code:1;model=a=b;session=s"
    assert trailers.identity_key(value) == ("claude-code", "a=b", "s", "")
