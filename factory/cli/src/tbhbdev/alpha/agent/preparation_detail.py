"""Redact sensitive values from run preparation errors."""

import os
import re
import unicodedata

PREPARATION_CAUSE_LIMIT = 200
URL_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")
SECRET_DETAIL = re.compile(
    r"(?i)\b(?:gh[pousr]_[\w]+|github_pat_[\w]+)"
    r"|\b(?:authorization[=:]\s*(?:basic|bearer)\s+"
    r"|(?:token|password|secret)[=:]\s*)[^\s,'\"]+"
)
ABSOLUTE_PATH = re.compile(r"(?<![\w:/])/(?:[^\s/'\"]+/)*([^\s/'\"]+)")


def safe_preparation_detail(detail: str) -> str:
    detail = next(iter(detail.splitlines()), "")
    detail = "".join(c for c in detail if not unicodedata.category(c).startswith("C"))
    detail = URL_CREDENTIALS.sub(r"\1[redacted]@", detail)
    detail = SECRET_DETAIL.sub("[redacted]", detail)
    for value in sorted(set(os.environ.values()) - {""}, key=len, reverse=True):
        detail = detail.replace(value, "[redacted]")
    detail = ABSOLUTE_PATH.sub(r"\1", detail)
    return detail[:PREPARATION_CAUSE_LIMIT] or "cause unavailable"
