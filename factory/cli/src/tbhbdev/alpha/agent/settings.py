"""Reading the agent settings from the user config.

This is the only module that reads config. It reads the `[agent]` table once
and hands it to `decisions/config.py`, which validates it.
"""

from __future__ import annotations

import socket

from tbhbdev import userconfig
from tbhbdev.alpha.agent.decisions import config
from tbhbdev.alpha.agent.model import Invalid, Settings


def load() -> Settings:
    """Read and validate the `[agent]` table, which has no deployment defaults.

    Raises:
        userconfig.ConfigError: The file or the table is missing or unreadable,
            or a key is missing or invalid. The message starts with the file's
            path.
    """
    try:
        table = userconfig.table("agent")
    except (userconfig.ConfigError, OSError, UnicodeError) as exc:
        message = f"{userconfig.path()}: config cannot be read: {exc}"
        raise userconfig.ConfigError(message) from exc
    if table is None:
        message = f"{userconfig.path()}: [agent] table is required"
        raise userconfig.ConfigError(message)
    try:
        return config.parse(table)
    except Invalid as exc:
        message = f"{userconfig.path()}: {exc}"
        raise userconfig.ConfigError(message) from exc


def known_host_names(configured: Settings | None = None) -> set[str]:
    """Read host names from the agent configuration and the local machine."""
    try:
        local = socket.gethostname().partition(".")[0]
        if configured is not None:
            hosts = configured.hosts
        else:
            table = userconfig.optional_table("agent")
            hosts = config.hosts(table) if table is not None else ()
    except (userconfig.ConfigError, OSError, UnicodeError, Invalid) as exc:
        message = "cannot read known host names"
        raise userconfig.ConfigError(message) from exc
    return {local, *(host.name for host in hosts)}
