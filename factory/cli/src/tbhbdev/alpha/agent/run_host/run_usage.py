"""Read the tokens and cost a harness reports, from text it already wrote."""

from __future__ import annotations

import json
import re
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


def unreported() -> dict[str, Value]:
    return _record(source=NOT_REPORTED)


def parse(harness: Harness, log_tail: str, out: str) -> dict[str, Value]:
    """The usage record of a run, from the end of its log and from its output.

    A figure the harness did not report is `"not reported"`, never zero. Codex
    reports a total on its log; Claude reports a result object on the output
    when it ran with `--output-format json`; agy reports one on the output
    when it ran with `--output-format stream-json`. Text that holds none of
    these gives an all-unreported record, never an exception.
    """
    if harness is Harness.CODEX:
        return _codex(log_tail)
    return _result(out, "claude" if harness is Harness.CLAUDE else "agy")


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


def _record(
    *,
    source: str,
    input_tokens: object = None,
    output_tokens: object = None,
    total_tokens: object = None,
    cost_usd: object = None,
) -> dict[str, Value]:
    figures = {
        "input_tokens": _number(input_tokens),
        "output_tokens": _number(output_tokens),
        "total_tokens": _number(total_tokens),
        "cost_usd": _number(cost_usd),
    }
    return {
        key: NOT_REPORTED if value is None else value for key, value in figures.items()
    } | {"source": source}


def _codex(log_tail: str) -> dict[str, Value]:
    found = _CODEX_TOKENS.findall(log_tail)
    if not found:
        return unreported()
    try:
        total = int(found[-1].replace(",", ""))
    except ValueError:
        return unreported()
    return _record(source="codex log", total_tokens=total)


def _objects(text: str) -> Iterator[Json]:
    """The JSON objects of TEXT, last first: one document or one per line."""
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, list):
        for element in reversed(cast("list[object]", whole)):
            if isinstance(element, dict):
                yield cast("Json", element)
    elif isinstance(whole, dict):
        yield cast("Json", whole)
    else:
        for line in reversed(text.splitlines()):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                yield cast("Json", item)


def _result(text: str, source: str) -> dict[str, Value]:
    for item in _objects(text):
        # Claude puts usage on the result; agy nests it under `result`.
        body = item.get("result") if item.get("event") == "result" else item
        if not isinstance(body, dict) or not isinstance(body.get("usage"), dict):
            continue
        usage = cast("Json", body["usage"])
        input_tokens = _number(usage.get("input_tokens"))
        output_tokens = _number(usage.get("output_tokens"))
        total = _number(usage.get("total_tokens"))
        if total is None and input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens
        if input_tokens is None and output_tokens is None and total is None:
            continue
        return _record(
            source=f"{source} result",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            cost_usd=body.get("total_cost_usd"),
        )
    return unreported()
