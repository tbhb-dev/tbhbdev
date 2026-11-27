"""The recorded run and its serialized representation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.driver.decisions.hosts import select_host
from tbhbdev.alpha.agent.model import NAME, Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, RunRequest, Settings


class Run:
    """The recorded target and request, with no settings or credential references."""

    def __init__(
        self,
        name: str,
        host: Host,
        run: RunRequest,
        started: str,
        owner: str,
    ) -> None:
        self.name = name
        self.host = host
        self.request = run
        self.started = started
        self.owner = owner
        self.launched_by: tuple[str | None, str | None] = (None, None)
        self.ignore_limits = False


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        message = f"run name {name!r} must match {NAME.pattern}"
        raise Invalid(message)


def record_fields(run: Run) -> dict[str, object]:
    return {
        **request.to_dict(run.request),
        "host": run.host.name,
        "started": run.started,
        "owner": run.owner,
        "launcher": run.launched_by[0],
        "launcher_session": run.launched_by[1],
        "cleaning": False,  # Older coordinators expect this field.
        "ignore_limits": run.ignore_limits,
    }


def _maybe_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_record(settings: Settings, name: str, text: str) -> Run:
    record: dict[str, object] = json.loads(text)
    host = select_host(settings, str(record.pop("host")), str(record["account"]))
    started = str(record.pop("started"))
    owner = str(record.pop("owner"))
    record.pop("cleaning")
    launched_by = (
        _maybe_text(record.pop("launcher", None)),
        _maybe_text(record.pop("launcher_session", None)),
    )
    # Records from before the limits check have no such field.
    ignore_limits = record.pop("ignore_limits", False)
    run = request.parse(record, host_allows_unsandboxed=True)
    result = Run(name, host, run, started, owner)
    result.launched_by = launched_by
    result.ignore_limits = bool(ignore_limits)
    return result
