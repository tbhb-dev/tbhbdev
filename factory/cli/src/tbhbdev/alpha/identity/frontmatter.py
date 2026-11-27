# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Read and rewrite document frontmatter attribution."""

import re
from dataclasses import dataclass
from itertools import takewhile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


type Entry = dict[str, str]

DOC_FIELDS = (
    "role",
    "harness",
    "model",
    "effort",
    "surface",
    "session",
    "subagent",
    "at",
)

DOC_REQUIRED = frozenset({"role", "harness", "model", "at"})

HASHED_ID = r"sha-256-64:[0-9a-f]{16}"

# The contributor patterns of docs/schemas/frontmatter.schema.json. A surface is
# further limited to printable ASCII so that it always round-trips through YAML.
DOC_PATTERNS = {
    name: re.compile(pattern)
    for name, pattern in {
        "role": r"generated|assisted",
        "harness": r"(claude-code|codex|antigravity)(:[0-9A-Za-z][0-9A-Za-z._+-]*)?",
        "model": r"(anthropic|openai|gcp\.gemini):[0-9A-Za-z][0-9A-Za-z._-]*",
        "effort": r"[a-z0-9][a-z0-9._-]*",
        "surface": r"(?:(?![;,])[!-~])+",
        "session": HASHED_ID,
        "subagent": HASHED_ID,
        "at": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
    }.items()
}

PLAIN_SCALAR = re.compile(r"[A-Za-z]([A-Za-z0-9._:/+-]*[A-Za-z0-9._/+-])?")

YAML_WORDS = {"null", "true", "false", "yes", "no", "on", "off", "y", "n"}

ITEM_LINE = re.compile(r"( *)- ([a-z]+): (.*)")

FIELD_LINE = re.compile(r"( *)([a-z]+): (.*)")

QUOTED = re.compile(r'"((?:[^"\\]|\\["\\])*)"')

SINGLE_QUOTED = re.compile(r"'([^']*)'")

PLAIN_VALUE = re.compile(r"[A-Za-z0-9][^\s#]*")

ESCAPED = re.compile(r'\\(["\\])')

DEFAULT_INDENT = 2


@dataclass
class DocEntry:
    """One parsed ``generated`` list item: its lines and its field values."""

    first: int
    last: int
    indent: int
    fields: Entry


def valid_entry(entry: Mapping[str, str]) -> bool:
    """True when the entry is a complete contributor the schema accepts."""
    return (
        entry.keys() >= DOC_REQUIRED
        and entry.keys() <= DOC_PATTERNS.keys()
        and all(DOC_PATTERNS[name].fullmatch(value) for name, value in entry.items())
    )


type ContributorKey = tuple[str, str, str | None, str | None]


def contributor_key(entry: Mapping[str, str]) -> ContributorKey:
    """Who a contributor is: harness slug (no version), model, session, subagent."""
    return (
        entry["harness"].partition(":")[0],
        entry["model"],
        entry.get("session"),
        entry.get("subagent"),
    )


def yaml_scalar(value: str) -> str:
    """A YAML scalar for a value: plain when safe, double-quoted otherwise.

    A timestamp (``at``) starts with a digit, so it is always quoted and no YAML
    parser turns it into a timestamp.
    """
    if PLAIN_SCALAR.fullmatch(value) and value.lower() not in YAML_WORDS:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_entry(entry: Entry, indent: int) -> list[str]:
    """Block-style lines for one list item, fields in canonical order."""
    lines = []
    for number, name in enumerate(n for n in DOC_FIELDS if n in entry):
        lead = " " * indent + "- " if number == 0 else " " * (indent + 2)
        lines.append(f"{lead}{name}: {yaml_scalar(entry[name])}")
    return lines


def unquote(raw: str) -> str | None:
    """The string a plain or quoted YAML scalar holds, or None if unsupported."""
    value = raw.rstrip()
    if match := QUOTED.fullmatch(value):
        return ESCAPED.sub(r"\1", match[1])
    if match := SINGLE_QUOTED.fullmatch(value):
        return match[1]
    return value if PLAIN_VALUE.fullmatch(value) else None


def parse_entries(lines: list[str]) -> list[DocEntry] | None:
    """The list items in ``lines``; None if any line is not plain block YAML."""
    entries: list[DocEntry] = []
    for number, line in enumerate(lines):
        matched = ITEM_LINE.fullmatch(line)
        if matched:
            entries.append(DocEntry(number, number + 1, len(matched[1]), {}))
        else:
            matched = FIELD_LINE.fullmatch(line)
            if not (matched and entries and len(matched[1]) == entries[-1].indent + 2):
                return None
            entries[-1].last = number + 1
        value = unquote(matched[3])
        if value is None or matched[2] in entries[-1].fields:
            return None
        entries[-1].fields[matched[2]] = value
    return entries


def merge_entry(old: Entry, new: Entry) -> Entry:
    """``old`` updated with a newer or older sighting ``new`` of the same contributor.

    Generated beats assisted and never the reverse. Only a newer sighting moves
    ``at`` or replaces effort, surface and a harness version that is already
    known; an older one only fills what is missing.
    """
    if new["role"] == "assisted" and old["role"] == "generated":
        return old
    newer = new["at"] >= old["at"]
    merged = {**old, "role": new["role"]}
    if newer:
        merged["at"] = new["at"]
    for name in ("effort", "surface"):
        if name in new and (newer or name not in old):
            merged[name] = new[name]
    if ":" in new["harness"] and (newer or ":" not in old["harness"]):
        merged["harness"] = new["harness"]
    return merged


def frontmatter_end(lines: list[str]) -> int | None:
    """The index of the closing fence, or None without well-formed frontmatter."""
    if lines[0] != "---":
        return None
    return next(
        (n for n, line in enumerate(lines) if n and line.rstrip(" \t") == "---"), None
    )


def generated_key(lines: list[str], close: int) -> int:
    """The line of the top-level ``generated:`` key, added before the closing fence."""
    # Line 0 is the opening fence, which never starts with the key.
    found = next(
        (n for n, line in enumerate(lines[:close]) if line.startswith("generated:")),
        None,
    )
    if found is None:
        lines.insert(close, "generated:")
        return close
    return found


def latest_generation(entries: Iterable[Entry]) -> list[Entry]:
    """The valid contributors in ``entries``, each once, generated before assisted.

    Repeat sightings of one contributor are merged with ``merge_entry``.
    """
    merged: dict[ContributorKey, Entry] = {}
    for entry in entries:
        if valid_entry(entry):
            key = contributor_key(entry)
            merged[key] = merge_entry(merged[key], entry) if key in merged else entry
    return sorted(merged.values(), key=lambda e: e["role"] != "generated")


def stamp_text(text: str, entries: Iterable[Entry]) -> str:
    """``text`` with its frontmatter's ``generated`` list set to ``entries``.

    The list names only the latest generation: the contributors of this
    commit replace whatever it held, so it never grows past one commit's
    worth. Only the ``generated`` block changes; every other byte is
    preserved. The text comes back unchanged when there is nothing valid to
    stamp, when it has no (LF) frontmatter, when its ``generated`` value is
    not a plain block list, or when the list already holds exactly these
    contributors.
    """
    wanted = latest_generation(entries)
    lines = text.split("\n")
    close = frontmatter_end(lines)
    if not wanted or close is None:
        return text
    key = generated_key(lines, close)
    rest = lines[key].removeprefix("generated:").strip()
    if rest == "[]":
        lines[key] = "generated:"
    elif rest:
        return text
    body = takewhile(lambda line: line.startswith((" ", "- ")), lines[key + 1 :])
    stop = key + 1 + sum(1 for _ in body)
    parsed = parse_entries(lines[key + 1 : stop])
    if parsed is None:
        return text
    if [p.fields for p in parsed] == wanted:
        return text
    indent = parsed[0].indent if parsed else DEFAULT_INDENT
    lines[key + 1 : stop] = [
        line for entry in wanted for line in render_entry(entry, indent)
    ]
    return "\n".join(lines)
