"""Hook registrations for Claude Code, Codex and Antigravity."""

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from tbhbdev.alpha.install.model import Json


MARK = "# tbhbdev alpha install"
AGY_EDITS = (
    "write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content"
)
AGY_TIMEOUT = 30
AGY_PR_TIMEOUT = 120
HARNESS_TIMEOUT = 10
# Write out the default so the Codex trust hash covers the on-disk command.
CODEX_TIMEOUT = 600


def ours(entry: object) -> bool:
    """Whether a hook handler is one this command wrote."""
    if not isinstance(entry, dict):
        return False
    fields = cast("Json", entry)  # pragma: no mutate
    text = fields.get("command")
    return isinstance(text, str) and text.endswith(MARK)


def command(args: str) -> str:
    """A marked harness hook command that runs `tbhbdev alpha ARGS` from PATH."""
    return f"tbhbdev alpha {args} {MARK}"


def handler(args: str, timeout: int | None = None) -> Json:
    """One command hook handler."""
    entry: Json = {"type": "command", "command": command(args)}
    if timeout is not None:
        entry["timeout"] = timeout
    return entry


def group(matcher: str | None, *handlers: Json) -> Json:
    """A matcher group of handlers, for every tool when `matcher` is None."""
    entry: Json = {} if matcher is None else {"matcher": matcher}
    entry["hooks"] = list(handlers)
    return entry


def stop_hooks(harness: str) -> dict[str, list[Json]]:
    """The Stop event shared by the user and private run configurations."""
    timeout = {
        "claude": HARNESS_TIMEOUT,
        "codex": CODEX_TIMEOUT,
        "antigravity": AGY_TIMEOUT,
    }
    stop = handler(f"run-stop-hook {harness}", timeout[harness])
    return {"Stop": [stop if harness == "antigravity" else group(None, stop)]}


def claude_hooks() -> dict[str, list[Json]]:
    """Claude Code's hooks, the ones the tbhb-dev repositories used to carry."""
    record = handler("identity record claude")
    guard = handler("state-guard", HARNESS_TIMEOUT)
    return {
        **stop_hooks("claude"),
        "SessionStart": [
            group(
                None,
                handler("install --hook"),
                record,
                guard,
                handler("session-context", HARNESS_TIMEOUT),
            )
        ],
        "SubagentStart": [group(None, record)],
        "PreToolUse": [
            group("Bash|Agent", record),
            group("Bash", handler("guard-gh-pr")),
            group("Bash", handler("guard-op")),
            group("Write|Edit|MultiEdit|NotebookEdit", handler("guard-op")),
            group("Bash", handler("guard-gh-poll")),
            group("Write|Edit|MultiEdit|NotebookEdit", handler("guard-gh-poll")),
            group("Write|Edit|MultiEdit|NotebookEdit|Bash", handler("guard-scripts")),
            group("Bash|Write|Edit", guard),
            group("Bash", handler("guard-gh-config-dir")),
            group("Agent", handler("guard-agent-model")),
        ],
        "PostToolUse": [
            group("Write|Edit|MultiEdit|NotebookEdit|Bash", record),
            group("Write|Edit|MultiEdit", handler("pr hook claude")),
        ],
        "PostToolUseFailure": [group("Bash", record)],
        "PostModelSwitch": [group(None, record)],
    }


def codex_hooks() -> dict[str, list[Json]]:
    """Codex's hooks, the ones the tbhb-dev repositories used to carry."""
    record = handler("identity record codex", CODEX_TIMEOUT)
    return {
        **stop_hooks("codex"),
        "SessionStart": [group(None, record)],
        "SubagentStart": [group(None, record)],
        "PostToolUse": [
            group("apply_patch", record),
            group("apply_patch", handler("pr hook codex", CODEX_TIMEOUT)),
        ],
        "PreToolUse": [
            group("^Bash$", handler("guard-gh-pr", CODEX_TIMEOUT)),
            group("^Bash$", handler("guard-gh-poll", CODEX_TIMEOUT)),
            group("^Bash$", handler("guard-scripts", CODEX_TIMEOUT)),
            group("^apply_patch$", handler("guard-scripts", CODEX_TIMEOUT)),
            group("^(Bash|shell)$", handler("guard-op", CODEX_TIMEOUT)),
            group("apply_patch", handler("guard-op", CODEX_TIMEOUT)),
            group("^Bash$", handler("guard-gh-config-dir", CODEX_TIMEOUT)),
        ],
    }


def agy_hooks() -> Json:
    """Antigravity's named hook groups, the ones the repositories used to carry."""
    record = handler("identity record antigravity", AGY_TIMEOUT)
    return {
        "tbhbdev-alpha-run-stop": stop_hooks("antigravity"),
        "tbhbdev-alpha-identity": {
            "SessionStart": [handler("identity record antigravity")],
            "PreToolUse": [group("run_command", record)],
            "PostToolUse": [group(AGY_EDITS, record)],
        },
        "tbhbdev-alpha-op-account": {
            "PreToolUse": [
                group("run_command", handler("guard-op", AGY_TIMEOUT)),
                group(AGY_EDITS, handler("guard-op", AGY_TIMEOUT)),
            ],
        },
        "tbhbdev-alpha-pull-request": {
            "PreToolUse": [group("run_command", handler("guard-gh-pr", AGY_TIMEOUT))],
            "PostToolUse": [
                group(AGY_EDITS, handler("pr hook antigravity", AGY_PR_TIMEOUT))
            ],
        },
        "tbhbdev-alpha-gh-poll": {
            "PreToolUse": [group("run_command", handler("guard-gh-poll", AGY_TIMEOUT))],
        },
        "tbhbdev-alpha-gh-config-dir": {
            "PreToolUse": [
                group("run_command", handler("guard-gh-config-dir", AGY_TIMEOUT))
            ],
        },
        "tbhbdev-alpha-scripts": {
            "PreToolUse": [
                group("run_command|write_file", handler("guard-scripts", AGY_TIMEOUT))
            ],
        },
    }
