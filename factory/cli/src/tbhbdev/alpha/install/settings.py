"""Paths, merges, parsing and formatting for JSON harness settings."""

import difflib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.install.hooks import agy_hooks, claude_hooks, codex_hooks, ours
from tbhbdev.alpha.install.model import AGENT_ENVIRONMENT, CLAUDE, CODEX, InstallError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import PurePath

    from tbhbdev.alpha.install.model import Json


def config_path(harness: str, env: Mapping[str, str], home: Path) -> Path:
    """Where a harness reads its user-level hooks."""
    if harness == CLAUDE:
        base = env.get("CLAUDE_CONFIG_DIR") or home / ".claude"
        return Path(base) / "settings.json"
    if harness == CODEX:
        base = env.get("CODEX_HOME") or home / ".codex"
        return Path(base) / "hooks.json"
    return home / ".gemini" / "config" / "hooks.json"


def strip_ours(groups: list[object]) -> tuple[list[object], int | None]:
    """The groups without this command's handlers, and where its first one was.

    A group left with no handlers goes. Anything else stays as it was.
    """
    kept: list[object] = []
    first: int | None = None
    for entry in groups:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        fields = cast("Json", entry)  # pragma: no mutate
        handlers = fields.get("hooks")
        if not isinstance(handlers, list):
            kept.append(entry)
            continue
        listed = cast("list[object]", handlers)  # pragma: no mutate
        rest = [h for h in listed if not ours(h)]
        if len(rest) == len(listed):
            kept.append(entry)
            continue
        if first is None:
            first = len(kept)
        if rest:
            kept.append({**fields, "hooks": rest})
    return kept, first


def merge_events(config: Json, desired: Mapping[str, list[Json]]) -> Json:
    """Claude Code and Codex: `config` with its `hooks` events merged.

    This command's groups go where its first old one stood, or at the end.
    """
    current = config.get("hooks", {})
    if not isinstance(current, dict):
        message = "its hooks key is not an object"
        raise InstallError(message)
    hooks = dict(cast("Json", current))  # pragma: no mutate
    for event in [*hooks, *(e for e in desired if e not in hooks)]:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            message = f"its {event} hooks are not a list"
            raise InstallError(message)
        listed = cast("list[object]", groups)  # pragma: no mutate
        kept, first = strip_ours(listed)
        at = len(kept) if first is None else first
        merged = [*kept[:at], *desired.get(event, []), *kept[at:]]
        if merged or first is None:
            hooks[event] = merged
        else:
            del hooks[event]
    return {**config, "hooks": hooks}


def merged_config(harness: str, config: Json) -> Json:
    """A harness config with this command's hooks merged in."""
    if harness == CLAUDE:
        env = config.get("env", {})
        if not isinstance(env, dict):
            message = "its env key is not an object"
            raise InstallError(message)
        fields = cast("Json", env)  # pragma: no mutate
        merged = merge_events(config, claude_hooks())
        return {**merged, "env": {**fields, **AGENT_ENVIRONMENT}}
    if harness == CODEX:
        return merge_events(config, codex_hooks())
    return {**config, **agy_hooks()}


def parse_config(text: str | None) -> Json:
    """Parse JSON settings, treating missing or whitespace-only text as empty."""
    if text is None or not text.strip():
        return {}
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        message = f"it is not valid JSON ({exc})"
        raise InstallError(message) from exc
    if not isinstance(loaded, dict):
        message = "it is not a JSON object"
        raise InstallError(message)
    return cast("Json", loaded)  # pragma: no mutate


def format_config(config: Json) -> str:
    """Serialize harness settings with the installer's standard layout."""
    return json.dumps(config, indent=2) + "\n"


def config_diff(path: PurePath, old: str | None, new: str) -> str:
    """Format the unified diff from OLD (None when missing) to NEW."""
    before = [] if old is None else old.splitlines(keepends=True)
    diff = difflib.unified_diff(
        before, new.splitlines(keepends=True), str(path), str(path)
    )
    return "".join(diff)
