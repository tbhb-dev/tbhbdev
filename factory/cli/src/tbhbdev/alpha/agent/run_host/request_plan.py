"""Validate the run request against the selected host."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import config, request
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Claude, Host, RunRequest


def select_host(hosts: tuple[Host, ...], name: str) -> Host:
    host = next((h for h in hosts if h.name == name), None)
    if host is None:
        message = f"host {name!r} is not configured"
        raise Invalid(message)
    return host


def parse_request(raw: str, host: Host, claude: Claude) -> RunRequest:
    data = json.loads(raw)
    if not isinstance(data, dict):
        message = "request must be a JSON object"
        raise Invalid(message)
    parsed = request.parse(data)
    if not parsed.unsandboxed:
        message = "request unsandboxed: full access is required"
        raise Invalid(message)
    if parsed.account not in host.accounts:
        message = f"account {parsed.account!r} is not enabled on {host.name}"
        raise Invalid(message)
    if parsed.claude_profile is not None:
        profile = config.find_profile(claude.profiles, parsed.claude_profile)
        if profile is None:
            message = f"claude profile {parsed.claude_profile!r} is not configured"
            raise Invalid(message)
        parsed = dataclasses.replace(parsed, claude_profile=profile.name)
    return parsed
