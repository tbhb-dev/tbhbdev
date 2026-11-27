"""Resolve configured hosts and accounts for the coordinator."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, Os, Settings


def select_host(settings: Settings, name: str, account: str | None) -> Host:
    for host in settings.hosts:
        if host.name == name:
            chosen = host.account if account is None else account
            if chosen not in host.accounts:
                message = f"account {chosen!r} isn't one of the accounts of {name}"
                raise Invalid(message)
            return replace(host, account=chosen)
    message = f"host {name!r} isn't configured"
    raise Invalid(message)


def candidates(
    settings: Settings, system: Os, account: str | None, exclude: str | None
) -> list[Host]:
    return [
        host
        for host in settings.hosts
        if host.auto_select
        and host.os is system
        and host.name != exclude
        and host.max_concurrent_runs != 0
        and (account is None or account in host.accounts)
    ]
