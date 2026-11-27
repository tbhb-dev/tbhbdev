"""Merge the managed shell environment into Codex settings."""

import tomlkit
from tomlkit.items import InlineTable

from tbhbdev.alpha.install.model import AGENT_ENVIRONMENT, InstallError

POLICY = "shell_environment_policy"


def with_environment(old: str | None) -> str:
    """Codex's config.toml text OLD (None when missing) with AGENT_ENVIRONMENT.

    The environment goes in shell_environment_policy.set. tomlkit keeps every
    other key, comment and the file's layout.
    """
    try:
        document = tomlkit.parse(old or "")
    except ValueError as exc:
        message = f"it is not valid TOML ({exc})"
        raise InstallError(message) from exc
    if POLICY not in document:
        document[POLICY] = tomlkit.table()
    policy = document[POLICY]
    if not isinstance(policy, dict):
        message = f"its {POLICY} is not a table"
        raise InstallError(message)
    if "set" not in policy:
        inline = isinstance(policy, InlineTable)
        policy["set"] = tomlkit.inline_table() if inline else tomlkit.table()
    values: object = policy["set"]
    if not isinstance(values, dict):
        message = f"its {POLICY}.set is not a table"
        raise InstallError(message)
    values.update(AGENT_ENVIRONMENT)
    return document.as_string()
