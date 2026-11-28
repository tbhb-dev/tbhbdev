"""Usage is parsed from run text, and an absent figure is never a zero."""

# gazelle:include_pytest_conftest false

from __future__ import annotations

import json

import pytest

from tbhbdev.alpha.agent.model import Harness
from tbhbdev.alpha.agent.run_host import run_usage

NOT_REPORTED = "not reported"
NOTHING = dict.fromkeys(
    ("input_tokens", "output_tokens", "total_tokens", "cost_usd", "source"),
    NOT_REPORTED,
)
# The end of a real codex run log: the harness's last lines, then the launcher's.
CODEX_TAIL = (
    "The SHA-256 pin matches. `bazelisk run //:bazel_env` passed.\n"
    "tokens used\n"
    "54,534\n"
    "bazel shutdown: '/cache/_bazel_agent/19ccccc72632a8222a62005c6113d693' (rc=0)\n"
)
CLAUDE_RESULT = {
    "type": "result",
    "subtype": "success",
    "total_cost_usd": 0.0421,
    "usage": {
        "input_tokens": 120,
        "cache_read_input_tokens": 9000,
        "output_tokens": 45,
    },
}


def test_codex_total_is_read_and_the_rest_is_not_reported() -> None:
    assert run_usage.parse(Harness.CODEX, CODEX_TAIL, "") == NOTHING | {
        "total_tokens": 54534,
        "source": "codex log",
    }


@pytest.mark.parametrize(
    "tail", ["", "no count here\n", 'quoted "3 tokens used",\n', "tokens used\nmany\n"]
)
def test_codex_without_a_count_reports_nothing(tail: str) -> None:
    assert run_usage.parse(Harness.CODEX, tail, "") == NOTHING


def test_claude_result_gives_input_output_and_cost() -> None:
    assert run_usage.parse(Harness.CLAUDE, "", json.dumps(CLAUDE_RESULT)) == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 9165,
        "cost_usd": 0.0421,
        "source": "claude result",
    }


def test_claude_total_counts_the_cache_tokens() -> None:
    usage = {
        "input_tokens": 120,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 300,
        "output_tokens": 45,
    }
    text = json.dumps({"type": "result", "usage": usage})
    parsed = run_usage.parse(Harness.CLAUDE, "", text)
    assert (parsed["input_tokens"], parsed["total_tokens"]) == (120, 9465)


def test_claude_total_skips_a_cache_count_that_is_not_a_number() -> None:
    usage = CLAUDE_RESULT["usage"] | {"cache_creation_input_tokens": -5}
    text = json.dumps({"usage": usage})
    assert run_usage.parse(Harness.CLAUDE, "", text)["total_tokens"] == 9165


def test_claude_result_without_cache_tokens_keeps_input_plus_output() -> None:
    usage = {"input_tokens": 120, "output_tokens": 45}
    text = json.dumps({"usage": usage})
    assert run_usage.parse(Harness.CLAUDE, "", text)["total_tokens"] == 165


def test_agy_reports_nothing_even_for_a_result_object() -> None:
    text = json.dumps(CLAUDE_RESULT)
    assert run_usage.parse(Harness.AGY, "", text) == NOTHING


@pytest.mark.parametrize("harness", list(Harness))
@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain answer\n\nVERDICT: GO\n",
        "{not json",
        "[]",
        json.dumps([CLAUDE_RESULT]),
        json.dumps({"event": "step_update"}) + "\n" + json.dumps(CLAUDE_RESULT),
        '{"usage": "x"}',
        '{"type": "result", "usage": {"input_tokens": true, "output_tokens": -1}}',
    ],
)
def test_garbage_reports_nothing_and_never_raises(harness: Harness, text: str) -> None:
    assert run_usage.parse(harness, text, text) == NOTHING
