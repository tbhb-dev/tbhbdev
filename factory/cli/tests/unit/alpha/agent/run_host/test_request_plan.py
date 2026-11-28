"""Validate host requests from literal values."""

import json

import pytest
from agent_example import LINUX, settings
from test_alpha_agent_request import BUILD, CLAUDE

from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.run_host import request_plan


def test_select_host_uses_the_configured_name() -> None:
    config = settings()
    assert request_plan.select_host(config.hosts, LINUX.name) == LINUX
    with pytest.raises(Invalid, match="is not configured"):
        request_plan.select_host(config.hosts, "missing")


def test_claude_profile_uses_the_configured_spelling() -> None:
    data = CLAUDE | {"claude_profile": "PRIMARY", "unsandboxed": True}
    parsed = request_plan.parse_request(json.dumps(data), LINUX, settings().claude)
    assert parsed.claude_profile == "primary"


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ([], "request must be a JSON object"),
        (BUILD | {"kind": "start"}, "kind"),
        (BUILD | {"unsandboxed": False}, "full access is required"),
        (BUILD | {"unsandboxed": True, "account": "other"}, "is not enabled"),
        (
            CLAUDE | {"unsandboxed": True, "claude_profile": "missing"},
            "is not configured",
        ),
    ],
)
def test_request_refusals(value: object, error: str) -> None:
    with pytest.raises(Invalid, match=error):
        request_plan.parse_request(json.dumps(value), LINUX, settings().claude)
