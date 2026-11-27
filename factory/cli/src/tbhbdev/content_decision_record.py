"""Validate Markdown decision records."""

import re
from typing import cast

from tbhbdev.content_model import Json, normalize

_RECORD_SECTIONS = ("Context", "Decision", "Consequences")
_RECORD_OPTIONS = "Options considered"
_RECORD_SECTION_ORDERS = {
    _RECORD_SECTIONS,
    ("Context", "Decision", _RECORD_OPTIONS, "Consequences"),
}
_RECORD_WORD_LIMITS = {"Context": 150, "Decision": 120, "Consequences": 150}
_RECORD_TOTAL_LIMIT = 450
_RECORD_OPTION_LIMIT = 5
_RECORD_OPTION_WORD_LIMIT = 25
_RECORD_TITLE_MINIMUM = 4
# A closing quote must end a sentence, end the line, or precede attribution.
_RECORD_QUOTE = re.compile(
    r"""^[^\S\n]*(?:"[^"\n]*"|'[^\n]*?'|“[^”\n]*”|‘[^\n]*?’)"""  # noqa: RUF001
    r"""(?:(?<=[.!?,]["'”’])|(?=[^\S\n]*(?:$|[,.;!?—–-])))""",  # noqa: RUF001
    re.MULTILINE,
)


# Casts narrow the normalizer's JSON model for the type checker and do not
# change runtime values. Mutations to those casts can be equivalent.
def _record_words(value: Json) -> int:
    metrics = cast("Json", value["metrics"])  # pragma: no mutate
    visible = cast("dict[str, int]", metrics["visible"])  # pragma: no mutate
    return visible["words"]


def _record_paragraphs(blocks: list[Json], refusals: list[str]) -> None:
    for block in blocks:
        if block["kind"] != "paragraph":
            children = block.get("items", block.get("blocks", []))
            _record_paragraphs(
                cast("list[Json]", children),  # pragma: no mutate
                refusals,
            )
            continue
        span = cast("dict[str, dict[str, int]]", block["span"])  # pragma: no mutate
        line = span["start"]["line"]
        source = cast("str", block["source"])  # pragma: no mutate
        paragraph = source.rstrip("\r\n")
        if "\r" in paragraph or "\n" in paragraph:
            refusals.append(f"Line {line}: write each paragraph on one source line")
        visible = cast("str", block["visible"])  # pragma: no mutate
        if _RECORD_QUOTE.search(visible):
            refusals.append(f"Line {line}: start the line without a quoted sentence")


def _record_options(block: Json, content: list[Json], refusals: list[str]) -> None:
    metrics = cast("dict[str, int]", block["metrics"])  # pragma: no mutate
    if metrics["listItems"] > _RECORD_OPTION_LIMIT:
        refusals.append(f"{_RECORD_OPTIONS} exceeds {_RECORD_OPTION_LIMIT} bullets")
    if metrics["maxListDepth"] > 1 or any(
        group["kind"] != "list" or group["style"] != "unordered" for group in content
    ):
        refusals.append(f"{_RECORD_OPTIONS} must use flat bullet lists")
        return
    items = (
        item
        for group in content
        for item in cast("list[Json]", group["items"])  # pragma: no mutate
    )
    for index, item in enumerate(items, 1):
        if _record_words(item) > _RECORD_OPTION_WORD_LIMIT:
            refusals.append(f"Option {index} exceeds {_RECORD_OPTION_WORD_LIMIT} words")


def _record_structure(draft: Json, headings: tuple[object, ...]) -> list[str]:
    refusals: list[str] = []
    if headings not in _RECORD_SECTION_ORDERS:
        refusals.append(
            "Use these H2 sections in order: Context, Decision, "
            "optional Options considered, Consequences"
        )
    metrics = cast("dict[str, int]", draft["metrics"])  # pragma: no mutate
    if metrics["atxHeadings"] + metrics["setextHeadings"] != 1 + sum(
        heading is not None for heading in headings
    ):
        refusals.append("Use headings only for the H1 title and H2 sections")
    if metrics["tables"]:
        refusals.append("Remove tables")
    if metrics["fencedCodeBlocks"] or metrics["indentedCodeBlocks"]:
        refusals.append("Remove code blocks")
    if metrics["blockQuotes"]:
        refusals.append("Remove block quotes")
    if _record_words(draft) > _RECORD_TOTAL_LIMIT:
        refusals.append(f"Record body exceeds {_RECORD_TOTAL_LIMIT} words")
    return refusals


def check_decision_record(markdown: str) -> list[str]:
    """Return body refusals, or an empty list when the record passes."""
    try:
        # These fields describe the normalized draft. The check reads its body.
        draft = normalize(
            markdown,
            object="record",  # pragma: no mutate
            variant="decision-record",  # pragma: no mutate
            metadata={},  # pragma: no mutate
        )
    except ValueError as exc:
        return [str(exc)]
    blocks = cast("dict[str, Json]", draft["blocks"])  # pragma: no mutate
    order = cast("list[str]", draft["blockOrder"])  # pragma: no mutate
    sections = [blocks[key] for key in order[1:]]
    refusals = _record_structure(
        draft, tuple(block.get("heading") for block in sections)
    )
    title = blocks["title"]
    visible = cast("str", title["visible"])  # pragma: no mutate
    if _record_words(title) < _RECORD_TITLE_MINIMUM:
        refusals.append(f"Title needs at least {_RECORD_TITLE_MINIMUM} words")
    if visible.endswith((":", "?")):
        refusals.append("End the title without a colon or question mark")
    if _RECORD_QUOTE.search(visible):
        refusals.append("Start the title without a quoted sentence")
    for block in sections:
        heading = cast("str", block.get("heading", ""))  # pragma: no mutate
        limit = _RECORD_WORD_LIMITS.get(heading)
        if limit is not None and _record_words(block) > limit:
            refusals.append(f"{heading} exceeds {limit} words")
        content = cast("list[Json]", block["content"])  # pragma: no mutate
        if heading == _RECORD_OPTIONS:
            _record_options(block, content, refusals)
        _record_paragraphs(content, refusals)
    return refusals
