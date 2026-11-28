"""Transfer the CLI bundle and run inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    del settings, host, run, bundle, inputs, timeout
    message = "not built"
    raise Invalid(message)
