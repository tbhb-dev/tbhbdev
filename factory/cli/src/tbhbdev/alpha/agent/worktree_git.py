"""Run Git with host-controlled options during clone preparation."""

from __future__ import annotations

import base64
import os
import subprocess  # nosec B404
from functools import partial
lazy from pathlib import Path

from tbhbdev.alpha.agent import host_tools

GIT_OVERRIDES = (
    "gc.auto=0",
    "transfer.fsckObjects=true",
    "maintenance.auto=false",
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.sshCommand=ssh",
    "protocol.ext.allow=never",
    "core.pager=cat",
    "credential.helper=",
    "core.askPass=",
    "protocol.file.allow=user",
    "remote.origin.uploadpack=git-upload-pack",
    "fetch.recurseSubmodules=false",
    "submodule.recurse=false",
)


def git(clone: Path, *args: str, token: str | None = None) -> str:
    env = {key: os.environ[key] for key in ("HOME", "LANG") if key in os.environ}
    env.update(
        PATH=host_tools.search(),
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_ASKPASS="/bin/false",
        SSH_ASKPASS="/bin/false",
    )
    # ssh still reads the account's own ssh config, so its GitHub key applies.
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if token is not None:
        # The header travels in the environment, so no argument list shows it.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraheader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
        )
    command = [host_tools.resolve("git")]
    command += [item for value in GIT_OVERRIDES for item in ("-c", value)]
    directory = clone / ".git"
    command += (
        ["--git-dir", str(directory), "--work-tree", str(clone)]
        if directory.is_dir()
        else ["--git-dir", str(clone)]
    )
    # A moved repository answers with a redirect, and the owner gate checks only
    # the requested name, so an online command never follows one.
    offline = [*command, "-c", "protocol.allow=never"]
    online = args[0] in ("fetch", "ls-remote")
    invocation = [*command, "-c", "http.followRedirects=false"] if online else offline
    output = partial(
        subprocess.check_output,
        env=env,
        cwd="/",
        text=True,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    timeout = 300 if args[0] in ("fetch", "clone") else 30
    if args[0] == "init":
        return output([*invocation, *args]).strip()
    query = ("config", "--null", "--name-only", "--get-regexp", r"^(filter|merge)\.")
    try:
        keys = output([*offline, *query])
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        keys = ""
    invocation.extend((*driver_overrides(keys), *args))
    return output(invocation, timeout=timeout).strip()


def driver_overrides(keys: str) -> list[str]:
    values: list[str] = []
    for key in dict.fromkeys(keys.split("\0")):
        group, _, field = key.rpartition(".")
        if group.startswith("filter."):
            values.extend(f"{group}.{name}=" for name in ("smudge", "clean", "process"))
            values.append(f"{group}.required=false")
        elif group.startswith("merge.") and field == "driver":
            values.append(f"{group}.driver=")
    return [item for value in dict.fromkeys(values) for item in ("-c", value)]
