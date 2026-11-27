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
# Standalone shell commands begin a new instruction. Other line breaks can wrap prose.
PROSE_BREAK = re.compile(
    r"\n(?=[ \t]*(?:\n|[#>]|`{3,}|~{3,}|[-*+][ \t]|\d+[.)][ \t]|`*git[ \t]))"
)
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)", re.DOTALL)
CLAUSE_END = re.compile(
    # Keep comma-separated code arguments under their common prose introduction.
    rf"\bbut\b|[:;]|\.(?=\s|$)|\x00(?!\s*,\s*(?:or\s+)?[`\x00])"
    rf"|{PROSE_BREAK.pattern}|^[ \t]*`*git [^\n]*\n",
    re.IGNORECASE | re.MULTILINE,
)
FENCE = re.compile(r"^[ \t]*(?:```|~~~)[^\n]*(?:\n|$)", re.MULTILINE)
OTHER_TRAILER_OMISSION = re.compile(
    r"\b(?:no|without) (?:an? |the )?[`\"']?(?:signed-off-by|co-authored-by)[`\"']?,"
)
BRANCH_PREFIXES = ("agent", "feat", "feature", "fix", "hotfix", "release")
UNQUALIFIED_BRANCH_PREFIXES = tuple(f"{prefix}-" for prefix in BRANCH_PREFIXES)
QUALIFIED_REF_PREFIXES = (
    "refs/",
    "origin/",
    *(f"{prefix}/" for prefix in BRANCH_PREFIXES),
)
FILE_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]*$")


def normalize(text: str) -> str:
    """Fold Unicode, whitespace and case for every textual comparison."""
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def prohibiting(text: str, start: int, end: int | None = None) -> bool:
    """Look for a prohibition before the instruction or immediately after it."""
    # A soft line break continues prose. A fenced command has its own line.
    blocks = FENCE.split(text[:start])
    context = blocks[-1] if len(blocks) % 2 else blocks[-1].rsplit("\n", 1)[-1]
    line_break = text.rfind("\n", 0, start)
    if line_break >= 0 and PROSE_BREAK.match(text, line_break):
        context = context.rsplit("\n", 1)[-1]
    clause = CLAUSE_END.split(INLINE_CODE.sub("\0", context))[-1]
    clause = OTHER_TRAILER_OMISSION.sub("", normalize(clause))
    return NEGATION.search(clause) is not None or (
        end is not None and TRAILING_PROHIBITION.match(text, end) is not None
    )


def looks_like_checkout_path(target: str) -> bool:
    """Recognize path syntax in a plain checkout while preserving explicit refs."""
    return not target.startswith(QUALIFIED_REF_PREFIXES) and (
        "/" in target or FILE_EXTENSION.search(target) is not None
    )
