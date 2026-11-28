"""Usage is parsed from run text, and an absent figure is never a zero."""

# gazelle:include_pytest_conftest false

from __future__ import annotations

import json

import pytest

from tbhbdev.alpha.agent.model import Harness
from tbhbdev.alpha.agent.run_host import run_usage

NOT_REPORTED = "not reported"
NOTHING = {
    "input_tokens": NOT_REPORTED,
    "output_tokens": NOT_REPORTED,
    "total_tokens": NOT_REPORTED,
    "cost_usd": NOT_REPORTED,
    "source": NOT_REPORTED,
}
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
AGY_RESULT = {
    "event": "result",
    "result": {
        "status": "SUCCESS",
        "num_turns": 1,
        "usage": {
            "input_tokens": 8654,
            "output_tokens": 25,
            "thinking_tokens": 20,
            "cache_read_tokens": 0,
            "total_tokens": 8679,
        },
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
        "total_tokens": 165,
        "cost_usd": 0.0421,
        "source": "claude result",
    }


def test_agy_result_gives_its_usage_fields() -> None:
    text = json.dumps({"event": "step_update"}) + "\n" + json.dumps(AGY_RESULT) + "\n"
    assert run_usage.parse(Harness.AGY, "", text) == {
        "input_tokens": 8654,
        "output_tokens": 25,
        "total_tokens": 8679,
        "cost_usd": NOT_REPORTED,
        "source": "agy result",
    }


@pytest.mark.parametrize("harness", list(Harness))
@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain answer\n\nVERDICT: GO\n",
        "{not json",
        "[]",
        "null",
        '{"usage": "x"}',
        '{"type": "result", "usage": {"input_tokens": true, "output_tokens": -1}}',
        "\x00\xff",
    ],
)
def test_garbage_reports_nothing_and_never_raises(harness: Harness, text: str) -> None:
    assert run_usage.parse(harness, text, text) == NOTHING
