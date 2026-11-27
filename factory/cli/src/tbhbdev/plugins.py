"""Config files from other installed packages.

A package supplies config through an entry point in the `tbhbdev.plugins`
group. The entry point's `config(name: str) -> str | None` returns the
named file's text or None.

Plugins load on first use, not at import. A plugin that fails to load is
reported on stderr once and skipped, so the built-in commands keep working.
"""

from __future__ import annotations

import functools
import sys
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

GROUP = "tbhbdev.plugins"


class PluginError(Exception):
    """Two providers claim the same config file."""


@functools.cache
def load() -> tuple[tuple[str, object], ...]:
    """Each installed plugin's entry point name and object, in entry point order.

    A plugin whose entry point fails to load is reported and left out.
    """
    loaded: list[tuple[str, object]] = []
    for point in entry_points(group=GROUP):
        try:
            loaded.append((point.name, point.load()))
        # Loading runs the plugin's module code, which can raise anything.
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"tbhbdev: cannot load plugin {point.name!r}: "
                f"{type(exc).__name__}: {exc}\n"
            )
    return tuple(loaded)


def supplied(name: str) -> str | None:
    """The text of the config file NAME from the plugin that supplies it, if any.

    Raises:
        PluginError: More than one plugin supplies NAME.
    """
    found: list[tuple[str, str]] = []
    for plugin_name, plugin in load():
        reader: Callable[[str], str | None] | None = getattr(plugin, "config", None)
        text = reader(name) if reader is not None else None
        if text is not None:
            found.append((plugin_name, text))
    if len(found) > 1:
        names = ", ".join(repr(n) for n, _ in found)
        message = f"plugins {names} each supply {name}"
        raise PluginError(message)
    return found[0][1] if found else None
