# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity keys for attribution trailers."""

from tbhbdev.alpha.identity import trailers

ROOT = "sha-256-64:" + "a" * 16
CHILD = "sha-256-64:" + "c" * 16


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
