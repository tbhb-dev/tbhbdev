# cspell:ignore rustfsadmin ENOSYS
"""Classify RustFS startup logs without retaining diagnostic output."""

import re

VM_DATA_ROOT = "/var/lib/o11y-test"
VM_DATA_NAME = "probe"


def compose_data_dir(linux: bool) -> str | None:
    """Docker Desktop doesn't share this path, so it lives on the VM's own disk."""
    return None if linux else f"{VM_DATA_ROOT}/{VM_DATA_NAME}"


# A crashing server may never print its console line; check forbidden phrases first.
START_RULES = (
    (
        "default login",
        re.compile(r"rustfsadmin|default root credentials", re.IGNORECASE),
    ),
    ("unset credential", re.compile(r"is not set", re.IGNORECASE)),
    ("unsupported host", re.compile(r"ENOSYS|Function not implemented", re.IGNORECASE)),
    ("unsupported filesystem", re.compile(r"Unsupported filesystem", re.IGNORECASE)),
)


def start_log(text: str) -> str | None:
    """Return only the failing rule, never any of the log."""
    for name, pattern in START_RULES:
        if pattern.search(text):
            return name
    if '"console_enabled":false' not in text:
        return "console line missing"
    return None
