"""Decide how to interpret and act on run state."""

from __future__ import annotations

from tbhbdev.alpha.agent.model import Invalid


def launch(rc: int | None, *, launched: bool, expired: bool) -> str:
    if rc is not None:
        return "finished"
    if launched:
        return "launched"
    return "deadline" if expired else "poll"


def launch_deadline(phase: str, deadline: float, shim_seconds: int) -> float:
    return deadline + shim_seconds if phase == "shims" else deadline


def state(rc: int | None, *, session: bool) -> str:
    if rc is not None:
        return str(rc)
    return "running" if session else "unknown"


def wait(
    rc: int | None, *, session: bool, failed_seconds: float, unreachable: float
) -> int | None:
    if failed_seconds >= unreachable:
        return 75
    if rc is not None:
        return rc
    return None if session else 1


def kill(rc: int | None, *, session: bool, expired: bool) -> str:
    if rc is not None:
        return "finished"
    if not expired:
        return "poll"
    return "refuse" if session else "write"


def answer_state(rc: int | None) -> str:
    return "unknown" if rc is None else str(rc)


def clean(rc: int | None, *, session: bool, force: bool) -> None:
    if session:
        message = "run is running; clean refused"
        raise Invalid(message)
    if rc is None and not force:
        message = "run is unknown; clean requires --force"
        raise Invalid(message)
    if not force:
        message = (
            "full-access run may have unpublished commits or unread output; "
            "pass --force to clean anyway"
        )
        raise Invalid(message)
