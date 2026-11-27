"""Names and types shared by installer modules."""

PROG = "tbhbdev alpha install"


CLAUDE = "claude"
CODEX = "codex"
ANTIGRAVITY = "antigravity"
HARNESSES = (CLAUDE, CODEX, ANTIGRAVITY)


# Keep harness commands from prompting through the SSH agent.
AGENT_ENVIRONMENT = {"SSH_AUTH_SOCK": ""}


type Json = dict[str, object]


class InstallError(Exception):
    """A failure that stops the install, with the message to print."""
