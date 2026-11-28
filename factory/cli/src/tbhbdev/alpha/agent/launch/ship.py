"""Transfer the CLI bundle and run inputs."""

from __future__ import annotations

import io
import sys
import tarfile
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.launch.decisions import bootstrap
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Host, Settings
    from tbhbdev.alpha.agent.transport import Inputs


def send(  # noqa: PLR0913, PLR0917 -- launch transfer signature
    settings: Settings,
    host: Host,
    run: Run,
    bundle: bytes,
    inputs: Inputs,
    timeout: float,
) -> None:
    digest = run.cli_sha256
    if digest is None:
        message = f"{run.name}: cli_sha256 is required"
        raise Invalid(message)
    entries: list[tuple[str, bytes]] = [
        (f"{run.name}{files.REQUEST}", inputs.request.encode()),
        (f"{run.name}{files.PROMPT}", inputs.prompt.encode()),
    ]
    if inputs.schema is not None:
        entries.append((f"{run.name}{files.SCHEMA}", inputs.schema.encode()))
    entries.append((f"{run.name}.cli.tar", bundle))
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        for member, data in entries:
            header = tarfile.TarInfo(member)
            header.mode, header.size = 0o600, len(data)
            tar.addfile(header, io.BytesIO(data))
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    transport.shell(
        settings,
        host,
        bootstrap.receive(run.name, settings.sockets_dir, digest, python),
        transport.Stdin(inputs.owner, payload.getvalue()),
        timeout=timeout,
    )
