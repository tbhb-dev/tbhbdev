"""Normalize brief text and recognize common prohibitions."""

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
CLAUSE_END = re.compile(r"\bbut\b|[.;\n]", re.IGNORECASE)
PROSE_BREAK = re.compile(r"\n(?=\n|[#>]|```|~~~|[-*+] |\d+[.)] )")
OTHER_TRAILER_OMISSION = re.compile(
    r"\b(?:no|without) (?:an? |the )?[`\"']?(?:signed-off-by|co-authored-by)[`\"']?,"
)


def normalize(text: str) -> str:
    """Fold Unicode, whitespace and case for every textual comparison."""
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def prohibiting(text: str, start: int, end: int | None = None) -> bool:
    """Look for a prohibition before the instruction or immediately after it."""
    clause = CLAUSE_END.split(text[:start])[-1]
    clause = OTHER_TRAILER_OMISSION.sub("", normalize(clause))
    return NEGATION.search(clause) is not None or (
        end is not None and TRAILING_PROHIBITION.match(text, end) is not None
    )
