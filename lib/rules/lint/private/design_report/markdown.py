# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

from html import escape
from typing import TYPE_CHECKING

from .graph import LINE_LIMIT

if TYPE_CHECKING:
    from .graph import ModuleMetrics
    from .metrics import Report


def totals_table(base: dict[str, int], head: dict[str, int]) -> str:
    rows = ["| Metric | Base | Head | Difference |", "| --- | ---: | ---: | ---: |"]
    for metric in sorted(base.keys() | head.keys()):
        old, new = base.get(metric, 0), head.get(metric, 0)
        rows.append(f"| {metric.replace('_', ' ')} | {old} | {new} | {new - old:+d} |")
    return "\n".join(rows)


def label(name: str) -> str:
    safe = escape(name).replace("`", "&#96;").replace("\n", " ")
    return f"<code>{safe}</code>"


def module_limits(metrics: ModuleMetrics) -> list[str]:
    limits = []
    if metrics["lines"] > LINE_LIMIT:
        limits.append(f"{metrics['lines']} lines (limit {LINE_LIMIT})")
    if metrics["initializer_statements"]:
        limits.append(f"{metrics['initializer_statements']} initializer statements")
    return limits


def changed_row(name: str, old: ModuleMetrics, new: ModuleMetrics) -> str:
    status = " (new)" if not old else " (removed)" if not new else ""
    changes = [
        f"{key.replace('_', ' ')} {old.get(key, 0):g} → {new.get(key, 0):g}"
        for key in sorted(old.keys() | new)
        if old.get(key, 0) != new.get(key, 0)
    ]
    detail = f" — {', '.join(changes)}" if changes else ""
    return f"- {label(name)}{status}{detail}"


def difference(base: Report, head: Report) -> str:
    result = ["# CLI design report", "", totals_table(base["totals"], head["totals"])]
    before, after = base["modules"], head["modules"]
    rows = [
        changed_row(name, before.get(name, {}), after.get(name, {}))
        for name in sorted(before.keys() | after)
        if before.get(name) != after.get(name)
    ]
    if rows:
        result.extend(("", "## Changed modules", "", *rows))
    offenders = [
        f"- {label(name)} — {', '.join(limits)}"
        for name in sorted(after.keys() - before)
        if (limits := module_limits(after[name]))
    ]
    if offenders:
        result.extend(("", "## New modules over limits", "", *offenders))
    return "\n".join(result) + "\n"
