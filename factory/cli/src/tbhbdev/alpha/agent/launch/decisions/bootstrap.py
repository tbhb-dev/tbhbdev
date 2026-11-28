"""Commands for receiving, starting, and removing the CLI bundle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, Profile

_BOOTSTRAP_UNBUILT = "not built"


def receive(name: str, sockets_dir: str, sha256: str, python: str) -> str:
    del name, sockets_dir, sha256, python
    raise Invalid(_BOOTSTRAP_UNBUILT)


def start(
    name: str, sockets_dir: str, host_name: str, *, steer: bool = False
) -> tuple[str, ...]:
    del name, sockets_dir, host_name, steer
    raise Invalid(_BOOTSTRAP_UNBUILT)


def check_slot(name: str, host: Host, profile: Profile) -> str:
    del name, host, profile
    raise Invalid(_BOOTSTRAP_UNBUILT)


def remove_cli(name: str) -> str:
    del name
    raise Invalid(_BOOTSTRAP_UNBUILT)
