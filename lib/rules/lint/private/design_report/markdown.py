# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

from html import escape

from graph import LINE_LIMIT, Counts
from metrics import Report


def totals_table(base: Counts, head: Counts) -> str:
    rows = ["| Metric | Base | Head | Difference |", "| --- | ---: | ---: | ---: |"]
    for metric in sorted(base.keys() | head.keys()):
        old, new = base.get(metric, 0), head.get(metric, 0)
        rows.append(f"| {metric.replace('_', ' ')} | {old} | {new} | {new - old:+d} |")
    return "\n".join(rows)


def difference(base: Report, head: Report) -> str:
    if base == head:
        return "No design metrics changed.\n"
    before, after = base["modules"], head["modules"]
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}

    def severity(name: str) -> tuple[float, ...]:
        old, new = before.get(name, {}), after.get(name, {})
        return (
            new.get("lines", 0) > LINE_LIMIT,
            new.get("initializer_statements", 0) > 0,
            new.get("lines", 0) - old.get("lines", 0),
            new.get("fan_out", 0) - old.get("fan_out", 0),
            new.get("instability", 0) - old.get("instability", 0),
            old.get("test_files", 0) - new.get("test_files", 0),
        )

    result = ["# CLI design report", "", totals_table(base["totals"], head["totals"])]
    if changed:
        result.extend(("", "## Changed modules", ""))
    for name in sorted(sorted(changed), key=severity, reverse=True):
        old, new = before.get(name, {}), after.get(name, {})
        changes = [
            f"{key.replace('_', ' ')} {old.get(key, 0):g} → {new.get(key, 0):g}"
            for key in sorted(old.keys() | new.keys())
            if old.get(key, 0) != new.get(key, 0)
        ]
        status = " (new)" if name not in before else " (removed)" if name not in after else ""
        detail = ", ".join(changes)
        label = escape(name).replace("\x60", "&#96;").replace("\n", " ")
        result.append(f"- <code>{label}</code>{status}" + (f" — {detail}" if detail else ""))

    offenders = [name for name in changed - before.keys() if any(severity(name)[:2])]
    if offenders:
        result.extend(("", "## New modules over limits", ""))
    for name in sorted(offenders):
        metrics = after[name]
        limits = []
        if metrics["lines"] > LINE_LIMIT:
            limits.append(f"{metrics['lines']} lines (limit {LINE_LIMIT})")
        if metrics["initializer_statements"]:
            limits.append(f"{metrics['initializer_statements']} initializer statements")
        label = escape(name).replace("\x60", "&#96;").replace("\n", " ")
        result.append(f"- <code>{label}</code> — {', '.join(limits)}")
    return "\n".join(result) + "\n"
