"""Sanitize and label failures during run preparation."""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import unicodedata
from contextlib import contextmanager
from pathlib import Path
lazy from collections.abc import Generator

from tbhbdev.alpha.agent.model import Invalid

PREPARATION_CAUSE_LIMIT = 200
URL_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")
SECRET_DETAIL = re.compile(
    r"(?i)\b(?:gh[pousr]_[\w]+|github_pat_[\w]+)"
    r"|\b(?:authorization[=:]\s*(?:basic|bearer)\s+"
    r"|(?:token|password|secret)[=:]\s*)[^\s,'\"]+"
)
ABSOLUTE_PATH = re.compile(r"(?<![\w:/])/(?:[^\s/'\"]+/)*([^\s/'\"]+)")


@contextmanager
def preparation_step(step: str) -> Generator[None]:
    try:
        yield
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        message = f"run preparation failed at {step}: {preparation_cause(exc)}"
        raise Invalid(message) from None


def preparation_cause(exc: OSError | ValueError | subprocess.SubprocessError) -> str:
    if isinstance(exc, Invalid):
        detail = str(exc)
    elif isinstance(exc, OSError):
        detail = exc.strerror or str(exc)
        if exc.filename is not None:
            detail += f": {Path(os.fsdecode(exc.filename)).name}"
    elif isinstance(exc, subprocess.SubprocessError):
        detail = getattr(exc, "stderr", None) or "git command failed"
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
    else:
        # Other ValueErrors can contain third-party inputs or credential values.
        detail = "invalid value"
    return safe_preparation_detail(detail)


def safe_preparation_detail(detail: str) -> str:
    detail = next(iter(detail.splitlines()), "")
    detail = "".join(c for c in detail if not unicodedata.category(c).startswith("C"))
    detail = URL_CREDENTIALS.sub(r"\1[redacted]@", detail)
    detail = SECRET_DETAIL.sub("[redacted]", detail)
    for value in sorted(set(os.environ.values()) - {""}, key=len, reverse=True):
        detail = detail.replace(value, "[redacted]")
    detail = ABSOLUTE_PATH.sub(r"\1", detail)
    return detail[:PREPARATION_CAUSE_LIMIT] or "cause unavailable"
