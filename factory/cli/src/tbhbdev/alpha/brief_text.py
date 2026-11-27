"""Read prohibitions and branch references from brief text."""

import re
import unicodedata

NEGATION = re.compile(
    r"(?<![\w-])(?:never|no|not|don['\u2019]t|without|avoid|forbidden)\b"
)
TRAILING_PROHIBITION = re.compile(
    r"[ \t`\"')]*(?:(?:is|are) (?:forbidden|prohibited|not allowed)"
    r"|(?:must|should) (?:not|never))\b",
    re.IGNORECASE,
)
PROSE_BREAK = re.compile(r"\n(?=[ \t]*(?:\n|[#>]|```|~~~|[-*+] |\d+[.)] ))")
CLAUSE_END = re.compile(
    rf"\bbut\b|[.;]|{PROSE_BREAK.pattern}|^[ \t]*`?git [^\n]*\n",
    re.IGNORECASE | re.MULTILINE,
)
FENCE = re.compile(r"^[ \t]*(?:```|~~~)[^\n]*(?:\n|$)", re.MULTILINE)
INTRODUCTION = re.compile(r":(?= |$)")
NEGATED_INTRODUCTION = re.compile(
    r"\b(?:(?:do not|don['\u2019]t|never|avoid)(?: (?:run|execute|use))?"
    r"|(?:forbidden|prohibited)(?: commands?)?)$"
)
OTHER_TRAILER_OMISSION = re.compile(
    r"\b(?:no|without) (?:an? |the )?[`\"']?(?:signed-off-by|co-authored-by)[`\"']?,"
)
CHECKOUT_PATH = re.compile(r"^(?:\./|\.\./|/)|\.[A-Za-z][A-Za-z0-9]*$")


def normalize(text: str) -> str:
    """Fold Unicode, whitespace and case for every textual comparison."""
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def prohibiting(text: str, start: int, end: int | None = None) -> bool:
    """Look for a prohibition before the instruction or immediately after it."""
    # A soft line break continues prose. A fenced command has its own line.
    blocks = FENCE.split(text[:start])
    context = blocks[-1] if len(blocks) % 2 else blocks[-1].rsplit("\n", 1)[-1]
    clause = CLAUSE_END.split(context)[-1]
    clause = OTHER_TRAILER_OMISSION.sub("", normalize(clause))
    # A label can introduce a prohibition or start a new instruction.
    *labels, clause = INTRODUCTION.split(clause)
    return (
        (bool(labels) and NEGATED_INTRODUCTION.search(labels[-1]) is not None)
        or NEGATION.search(clause) is not None
        or (end is not None and TRAILING_PROHIBITION.match(text, end) is not None)
    )


def looks_like_checkout_path(target: str) -> bool:
    """Recognize path syntax in a plain checkout while preserving explicit refs."""
    return (
        not target.startswith(("refs/", "agent/", "origin/"))
        and CHECKOUT_PATH.search(target) is not None
    )
