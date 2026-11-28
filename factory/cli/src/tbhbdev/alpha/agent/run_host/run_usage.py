"""Read the tokens and cost a harness reports, from text it already wrote."""

from __future__ import annotations

import json
import re

from tbhbdev.alpha.agent.model import Harness

NOT_REPORTED = "not reported"
LOG_TAIL_BYTES = 8 * 1024
type Value = int | float | str

# Codex prints the count on the line after the label, with thousands separators.
_CODEX_TOKENS = re.compile(r"^tokens used\n([0-9][0-9,]*)$", re.MULTILINE)


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


def _record(source: str, **figures: object) -> dict[str, Value]:
    keys = ("input_tokens", "output_tokens", "total_tokens", "cost_usd")
    numbers = {key: _number(figures.get(key)) for key in keys}
    return {k: NOT_REPORTED if v is None else v for k, v in numbers.items()} | {
        "source": source
    }


def unreported() -> dict[str, Value]:
    return _record(NOT_REPORTED)


def parse(harness: Harness, log_tail: str, out: str) -> dict[str, Value]:
    """The usage record of a run, from the end of its log and from its output.

    A figure the harness did not report is `"not reported"`, never zero. Codex
    reports a total on its log. Claude reports a result object on the output
    when it ran with `--output-format json`. Any other harness, and text that
    is not one such object, gives an all-unreported record, never an exception.
    """
    if harness is Harness.CODEX:
        found = _CODEX_TOKENS.findall(log_tail)
        if not found:
            return unreported()
        return _record("codex log", total_tokens=int(found[-1].replace(",", "")))
    if harness is Harness.CLAUDE:
        return _result(out)
    return unreported()


def _result(text: str) -> dict[str, Value]:
    try:
        item = json.loads(text)
    except ValueError:
        return unreported()
    usage = item.get("usage") if isinstance(item, dict) else None
    if not isinstance(usage, dict):
        return unreported()
    input_tokens = _number(usage.get("input_tokens"))
    output_tokens = _number(usage.get("output_tokens"))
    total = _number(usage.get("total_tokens"))
    if total is None and None not in (input_tokens, output_tokens):
        # Claude counts the cache apart from `input_tokens`.
        counted = (
            input_tokens,
            output_tokens,
            _number(usage.get("cache_read_input_tokens")),
            _number(usage.get("cache_creation_input_tokens")),
        )
        total = sum(count for count in counted if count is not None)
    if {input_tokens, output_tokens, total} == {None}:
        return unreported()
    return _record(
        "claude result",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        cost_usd=item.get("total_cost_usd"),
    )
