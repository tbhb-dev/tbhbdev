"""Render the remote shell command carried by SSH."""

import shlex


def ssh_command(shell: str, remote: tuple[str, ...]) -> str:
    """Quote the remote argv for the configured shell."""
    return shlex.join((f"/bin/{shell}", "-c", shlex.join(remote)))
