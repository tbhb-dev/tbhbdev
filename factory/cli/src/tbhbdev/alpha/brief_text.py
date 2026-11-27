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
# A command can wrap after an introduction or start a new instruction on its own line.
PROSE_BREAK = re.compile(
    r"[ \t]*\n(?=[ \t]*(?:\n|[#>]|`{3,}|~{3,}|[-*+][ \t]|\d+[.)][ \t]))"
    r"|(?<![ \t])(?<!\b(?:not|run|use|and))(?<!\bor)"
    r"(?<!\b(?:never|avoid|don['\u2019]t))(?<!\bexecute)"
    r"[ \t]*\n(?=[ \t]*(?:(?:run|execute|use)[ \t]+)?`*git[ \t])",
    re.IGNORECASE,
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
    r"\b(?:no|without)\s+(?:(?:an?|the)\s+)?"
    r"[`\"']?(?:signed-off-by|co-authored-by)[`\"']?,",
    re.IGNORECASE,
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
    prefix = text[:start]
    blocks = FENCE.split(prefix)
    context = blocks[-1] if len(blocks) % 2 else blocks[-1].rsplit("\n", 1)[-1]
    previous, newline, _ = prefix.rpartition("\n")
    # Inspect the current line as well so a command at its start is visible.
    if newline and PROSE_BREAK.match(text, len(previous.rstrip(" \t"))):
        context = context.rsplit("\n", 1)[-1]
    context = OTHER_TRAILER_OMISSION.sub("", context)
    clause = CLAUSE_END.split(INLINE_CODE.sub("\0", context))[-1]
    return NEGATION.search(normalize(clause)) is not None or (
        end is not None and TRAILING_PROHIBITION.match(text, end) is not None
    )


def looks_like_checkout_path(target: str) -> bool:
    """Recognize path syntax in a plain checkout while preserving explicit refs."""
    return not target.startswith(QUALIFIED_REF_PREFIXES) and (
        "/" in target or FILE_EXTENSION.search(target) is not None
    )
