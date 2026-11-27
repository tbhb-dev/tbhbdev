"""The argv of each command a run starts: SSH commands.

Builders return tuples of strings; executable lookup uses a fixed system PATH.
No argv holds a credential or a Connect reference.
"""

from __future__ import annotations

import shlex
import shutil
import socket
import sys
from pathlib import Path

from tbhbdev.alpha.agent.model import (
    Host,
    Invalid,
)


def executable(name: str) -> str:
    path = "/usr/bin:/bin:/usr/sbin:/sbin"
    if sys.platform == "darwin":
        path += ":/opt/homebrew/bin:/usr/local/bin"
    found = shutil.which(name, path=path)
    if found is None:
        message = f"required executable {name!r} missing from fixed PATH"
        raise Invalid(message)
    return found


def ssh(
    host: Host,
    remote: tuple[str, ...],
    *,
    connect_timeout: int,
    keepalive: int,
    keepalive_count_max: int = 3,
) -> tuple[str, ...]:
    """The driver's call to HOST, which runs REMOTE in the host's shell.

    It never prompts, gives up connecting after CONNECT_TIMEOUT seconds and
    probes the connection every KEEPALIVE seconds. When present, the local
    machine's session key is the only identity offered, without an SSH agent.
    """
    command = shlex.join((f"/bin/{host.shell}", "-c", shlex.join(remote)))
    key = (
        Path.home()
        / ".ssh"
        / "sessions"
        / f"{socket.gethostname().split('.')[0]}_ed25519"
    )
    identity = (
        ("-i", str(key), "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes")
        if key.is_file()
        else ()
    )
    return (
        executable("ssh"),
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        f"ServerAliveInterval={keepalive}",
        "-o",
        f"ServerAliveCountMax={keepalive_count_max}",
        *identity,
        "-l",
        host.account,
        host.name,
        command,
    )
