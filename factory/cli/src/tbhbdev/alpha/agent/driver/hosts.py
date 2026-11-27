"""Probe capacity and choose where to launch."""

from __future__ import annotations

import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from tbhbdev import telemetry
from tbhbdev.alpha.agent import admission, transport
from tbhbdev.alpha.agent.decisions import limits, remote_host
from tbhbdev.alpha.agent.driver.decisions.hosts import candidates, select_host
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    import argparse

    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Host, Os, Settings


def read_limits(
    settings: Settings,
    host: Host,
    profile: Profile,
    *,
    ignore: bool,
    timeout: float,
) -> tuple[limits.Reading | str, bool, str]:
    """Read and judge the host's load, memory, pressure and swap in one SSH call."""
    if host.max_concurrent_runs == 0:
        message = f"{host.name}: max_concurrent_runs is 0"
        raise Invalid(message)
    try:
        found = limits.parse(
            transport.shell(settings, host, remote_host.limits(), timeout=timeout)
        )
    except transport.TransportError as exc:
        found = str(exc)
    memory_mib = (
        settings.review_memory_mib
        if profile is Profile.REVIEW
        else settings.run_memory_mib
    )
    refuse, message = limits.decide(host, found, memory_mib, ignore=ignore)
    return found, refuse, message


def check_limits(
    settings: Settings,
    host: Host,
    profile: Profile,
    *,
    ignore: bool,
    timeout: float = 30,
) -> None:
    """Refuse or warn based on one host reading."""
    _, refuse, message = read_limits(
        settings, host, profile, ignore=ignore, timeout=timeout
    )
    if refuse:
        raise Invalid(message)
    if message:
        print(message, file=sys.stderr)


def usable_hosts(
    settings: Settings,
    system: Os,
    args: argparse.Namespace,
    deadline: float,
    *,
    exclude: str | None = None,
) -> list[tuple[float, Host, str]]:
    """Return eligible hosts in increasing order of 1-minute load."""
    usable: list[tuple[float, Host, str]] = []
    profile = Profile(args.profile)
    account = args.account
    eligible = candidates(settings, system, account, exclude)
    for index, candidate in enumerate(eligible):
        host = select_host(settings, candidate.name, account)
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        probe_deadline = now + min(30, remaining / (len(eligible) - index))
        found, refuse, message = read_limits(
            settings,
            host,
            profile,
            ignore=False,
            timeout=probe_deadline - now,
        )
        if refuse or not isinstance(found, limits.Reading):
            continue
        # An older host may lack the probe until version synchronization.
        # The launch still checks capacity when it claims a slot.
        slot_timeout = probe_deadline - time.monotonic()
        if slot_timeout > 0:
            with suppress(transport.TransportError, transport.CollisionError):
                slot = transport.shell(
                    settings,
                    host,
                    remote_host.check_slot(host, profile),
                    timeout=slot_timeout,
                )
                if slot.strip() == admission.SLOT_FULL:
                    continue
        usable.append((found.load, host, message))
    return sorted(usable, key=lambda entry: entry[0])


def select_exec_host(
    settings: Settings, args: argparse.Namespace, deadline: float
) -> Host:
    if args.ignore_limits:
        message = "--ignore-limits requires --host"
        raise Invalid(message)
    # Reserve half the launch wait for preflight and startup on the selected host.
    usable = usable_hosts(
        settings, args.os, args, deadline - settings.launch_wait_seconds / 2
    )
    if not usable:
        message = (
            f"no usable {args.os} hosts for automatic selection; "
            "check auto_select and host limits"
        )
        raise Invalid(message)
    _, host, message = usable[0]
    if message:
        print(message, file=sys.stderr)
    return host


def check_named_host(
    settings: Settings, args: argparse.Namespace, run: Run, deadline: float
) -> None:
    try:
        check_limits(
            settings,
            run.host,
            run.request.profile,
            ignore=run.ignore_limits,
            timeout=deadline - time.monotonic(),
        )
    except Invalid as exc:
        usable = usable_hosts(
            settings, run.host.os, args, deadline, exclude=run.host.name
        )
        names = ", ".join(host.name for _, host, _ in usable)
        message = f"{exc}; available alternatives: {names or 'none'}"
        raise Invalid(message) from exc


def launch_host(
    settings: Settings, args: argparse.Namespace, run: Run | None, deadline: float
) -> Host:
    try:
        if run is None:
            return select_exec_host(settings, args, deadline)
        check_named_host(settings, args, run, deadline)
    except Invalid:
        telemetry.attributes(**{"tbhb.agent.outcome": "refused"})
        raise
    return run.host
