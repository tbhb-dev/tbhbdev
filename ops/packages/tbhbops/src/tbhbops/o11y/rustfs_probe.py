# cspell:ignore rustfsadmin ENOSYS
"""Classify RustFS probe results without retaining diagnostic output."""

import re

VM_DATA_ROOT = "/var/lib/o11y-test"
VM_DATA_NAME = "probe"


def compose_data_dir(linux: bool) -> str | None:  # noqa: FBT001
    """Docker Desktop doesn't share this path, so it lives on the VM's own disk."""
    return None if linux else f"{VM_DATA_ROOT}/{VM_DATA_NAME}"


CRASH_FAILURE = (
    "an acknowledged write was lost or a listed key is unreadable after a kill"
)
SUCCESS = (
    "start log is clean and reports the console off",
    "ready on 9000 and nothing listens on 9001",
    "a request signed with the default login is refused",
    (
        "1 KiB, 10 MiB and multipart 100 MiB round trips match, "
        "1,100 keys list across pages, delete empties the bucket"
    ),
    "",
    "probe bucket removed",
)


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


def survivors(text: str) -> str | None:
    """Accept one positive acknowledgement count, with no adjacent log text."""
    matches = re.findall(
        r"^probe: ([0-9]+) acknowledged writes survived$", text, re.MULTILINE
    )
    if len(matches) != 1 or int(matches[0]) == 0:
        return None
    return f"{int(matches[0])} acknowledged writes survived"


def basename(name: str) -> str:
    """Constrain utility-container paths to fixed names beneath their mount."""
    if not name or "/" in name or ".." in name:
        message = "unsafe probe basename"
        raise ValueError(message)
    return name
