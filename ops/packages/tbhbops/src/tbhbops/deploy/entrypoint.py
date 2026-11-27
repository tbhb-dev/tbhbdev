"""Authenticate deployment requests and start the receiver."""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import TextIO

from tbhbops.deploy.command import Command, CommandError
from tbhbops.deploy.compose import VERSION, Engine, verify_binary
from tbhbops.deploy.config import load, service, text
from tbhbops.deploy.protocol import RefusalError, Request, mapping, request, require
from tbhbops.deploy.receiver import Receiver

CONFIG = Path("/etc/tbhb/deploy")


def identity(rollback: str | None) -> Request:
    require(
        os.geteuid() == 0,
        "receive requires root",
    )
    if rollback is None:
        require(
            os.environ.get("SUDO_USER") == "deploy",
            "receive requires deploy sudo user",
        )
        wanted = request(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    else:
        require(
            os.environ.get("SUDO_USER") != "deploy",
            "deploy cannot request rollback",
        )
        require(
            re.fullmatch(r"[a-z][a-z0-9-]{0,31}", rollback) is not None,
            "rollback service",
        )
        wanted = Request(rollback, "0" * 40, None)
    return wanted


def identity_fields(
    wanted: Request | None,
) -> tuple[str | None, str | None, str | None]:
    if wanted is None:
        return None, None, None
    return wanted.service, wanted.release, wanted.run_id


def receive(out: TextIO, *, rollback: str | None = None) -> int:
    require(sys.platform != "win32", "receive requires Unix")
    started = time.monotonic()
    # None and the empty-string mutant are both false before assignment.
    wanted: Request | None = None  # pragma: no mutate
    try:
        wanted = identity(rollback)
        allowed = service(
            load(CONFIG / "services" / f"{wanted.service}.toml"), wanted.service
        )
        receiver = mapping(
            load(CONFIG / "receiver.toml"), {"compose_binary", "compose_version"}
        )
        binary = text(receiver.get("compose_binary"))
        require(
            Path(binary).is_absolute(),
            "Compose binary path",
        )
        require(
            receiver.get("compose_version") == VERSION,
            "unsupported Compose version",
        )
        verify_binary(Path(binary))
        command = Command()
        require(
            command(
                [binary, "version", "--short"],
                timeout_s=10,
                stdout_limit=4096,
                stderr_limit=4096,
            )
            .decode()
            .strip()
            == VERSION,
            "Compose version mismatch",
        )
        return Receiver(
            wanted, allowed, Engine(allowed, binary, command), out, started
        ).run(manual=rollback is not None)
    except (
        RefusalError,
        OSError,
        ValueError,
        CommandError,
        KeyError,
        TypeError,
    ) as error:
        recovery_failed = rollback is not None and isinstance(
            error, (KeyError, TypeError)
        )
        reason = str(error) if isinstance(error, RefusalError) else "request refused"
        stack, release, run_id = identity_fields(wanted)
        record = {
            "stack": stack,
            "release": release,
            "outcome": "failed" if recovery_failed else "refused",
            "step": "recovery" if recovery_failed else "request",
            "duration_s": round(time.monotonic() - started, 1),
            "run_id": run_id,
            "reason": "recovery failed" if recovery_failed else reason,
        }
        out.write(json.dumps(record) + "\n")
        return 3 if recovery_failed else 2
