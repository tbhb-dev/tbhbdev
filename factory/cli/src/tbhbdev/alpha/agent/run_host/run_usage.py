"""Read the tokens and cost a harness reports, from text it already wrote."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.agent.model import Harness

if TYPE_CHECKING:
    from collections.abc import Iterator

NOT_REPORTED = "not reported"
LOG_TAIL_BYTES = 8 * 1024
type Value = int | float | str
type Json = dict[str, object]

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
    when it ran with `--output-format json`. agy reports one when it ran with
    `--output-format stream-json`. Text that holds none of these gives an
    all-unreported record, never an exception.
    """
    if harness is Harness.CODEX:
        found = _CODEX_TOKENS.findall(log_tail)
        if not found:
            return unreported()
        return _record("codex log", total_tokens=int(found[-1].replace(",", "")))
    return _result(out, f"{harness.value} result")


def _objects(text: str) -> Iterator[Json]:
    """The JSON objects of TEXT, last first: one document or one per line."""
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, (dict, list)):
        items = whole if isinstance(whole, list) else [whole]
    else:
        items = []
        for line in text.splitlines():
            with suppress(ValueError):
                items.append(json.loads(line))
    yield from (
        cast("Json", item) for item in reversed(items) if isinstance(item, dict)
    )


def _result(text: str, source: str) -> dict[str, Value]:
    for item in _objects(text):
        # Claude puts usage on the result. agy nests it under `result`.
        body = item.get("result") if item.get("event") == "result" else item
        usage = body.get("usage") if isinstance(body, dict) else None
        if not isinstance(usage, dict):
            continue
        input_tokens = _number(usage.get("input_tokens"))
        output_tokens = _number(usage.get("output_tokens"))
        total = _number(usage.get("total_tokens"))
        if total is None and None not in (input_tokens, output_tokens):
            total = cast("float", input_tokens) + cast("float", output_tokens)
        if {input_tokens, output_tokens, total} != {None}:
            return _record(
                source,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total,
                cost_usd=item.get("total_cost_usd"),
            )
    return unreported()
