"""The offline gate for the tailnet policy, tailnet/policy.hujson.

Tailscale reads the policy as HuJSON: JSON plus comments and trailing
commas. The policy workflow runs the file's own tests against the tailnet,
which needs credentials, so this module checks only what holds offline:

1. The file parses as HuJSON.
2. Its first line is the header naming this repository, so anyone reading
   the policy in the admin console can see where a change has to go.
"""

import json
import re

HEADER = (
    "// This file is managed by https://github.com/tbhb-dev/tbhbdev, at "
    "tailnet/policy.hujson."
)

# A JSON string is kept whole, so nothing inside one reads as a comment or a
# comma. JSON strings can't hold a raw newline, so an unterminated one stops
# at the end of its line and the JSON parser then rejects it.
_STRING = r'("(?:[^"\\\n]|\\.)*")'
_COMMENT = re.compile(_STRING + r"|//[^\n]*|/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA = re.compile(_STRING + r"|,(\s*[\]}])")


def standardize(text: str) -> str:
    """Return HuJSON text as plain JSON, without parsing it.

    Comments become a space and a comma before a closing bracket or brace
    goes. Anything else HuJSON forbids is left for the JSON parser to reject.
    """
    without_comments = _COMMENT.sub(lambda m: m.group(1) or " ", text)
    return _TRAILING_COMMA.sub(lambda m: m.group(1) or m.group(2), without_comments)


def check(text: str) -> list[str]:
    """Return what is wrong with a policy file's text, or nothing."""
    problems: list[str] = []
    try:
        json.loads(standardize(text))
    except json.JSONDecodeError as error:
        problems.append(f"not valid HuJSON: {error}")
    if text.partition("\n")[0] != HEADER:
        problems.append(f"the first line must be exactly: {HEADER}")
    return problems
