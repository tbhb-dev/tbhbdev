"""The recorded run and its serialized representation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.driver.decisions.launch import select_host

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
        self.cleaning = False
        self.ignore_limits = False


def record_fields(run: Run) -> dict[str, object]:
    return {
        **request.to_dict(run.request),
        "host": run.host.name,
        "started": run.started,
        "owner": run.owner,
        "launcher": run.launched_by[0],
        "launcher_session": run.launched_by[1],
        "cleaning": run.cleaning,
        "ignore_limits": run.ignore_limits,
    }


def _maybe_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_record(settings: Settings, name: str, text: str) -> Run:
    record: dict[str, object] = json.loads(text)
    host = select_host(settings, str(record.pop("host")), str(record["account"]))
    started = str(record.pop("started"))
    owner = str(record.pop("owner"))
    cleaning = record.pop("cleaning")
    launched_by = (
        _maybe_text(record.pop("launcher", None)),
        _maybe_text(record.pop("launcher_session", None)),
    )
    # Records from before the limits check have no such field.
    ignore_limits = record.pop("ignore_limits", False)
    run = request.parse(record, host_allows_unsandboxed=True)
    result = Run(name, host, run, started, owner)
    result.launched_by = launched_by
    result.cleaning = bool(cleaning)
    result.ignore_limits = bool(ignore_limits)
    return result


def cleaning_record(text: str) -> str:
    record = json.loads(text)
    record["cleaning"] = True
    return json.dumps(record) + "\n"
