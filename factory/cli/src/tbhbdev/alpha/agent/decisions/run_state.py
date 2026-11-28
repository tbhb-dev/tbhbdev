"""Run lifecycle decisions from recorded facts, with no I/O."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    pgid: int
    boot: str
    start: str


def owns(recorded: Group, current: Group | None) -> bool:
    """Whether a live leader still has the recorded identity."""
    return recorded.pgid > 1 and recorded == current


# Phases before the harness starts. The group record in them belongs to the
# setup worker, which has exited, until the harness replaces it.
SETUP_PHASES = frozenset({"check", "workspace", "brief", "shims"})


def setup_record(phase: str | None, *, launched: bool) -> bool:
    """Whether the recorded group is the setup worker's, not the harness's."""
    return phase in SETUP_PHASES and not launched


def time_limit(timeout: int | None, maximum: int | None) -> int | None:
    """The run cap in seconds, limited by the request and configuration."""
    limits = [timeout]
    if maximum is not None:
        limits.append(maximum * 60)
    return min((n for n in limits if n is not None), default=None)


def exit_status(returncode: int) -> int:
    """Represent a signal exit as the shell's status, for the driver and index."""
    return 128 - returncode if returncode < 0 else returncode
