"""Send a validated message to the recorded run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote_host, remote_shell
from tbhbdev.alpha.agent.driver.decisions.steering import check_send
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.terminal import terminal_text

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def send(settings: Settings, run: Run, text: str) -> int:
    name = run.name
    check_send(name, run.request.harness, text)
    script = remote_shell.owned(name) + (
        f"exec {remote_host.program(run.host)} alpha agent send-host {name}\n"
    )
    error = transport.shell(
        settings, run.host, script, transport.Stdin(run.owner, text.encode())
    ).strip()
    if error:
        message = f"{name}: {terminal_text(error)}"
        raise Invalid(message)
    return 0
