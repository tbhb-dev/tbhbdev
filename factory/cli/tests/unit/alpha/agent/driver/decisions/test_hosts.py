"""Test selection of a configured coordinator host and account."""

from dataclasses import replace

import pytest
from agent_example import LINUX, settings

from tbhbdev.alpha.agent.driver.decisions.hosts import select_host
from tbhbdev.alpha.agent.model import Invalid


@pytest.mark.parametrize("account", [None, "agent", "ops"])
def test_host(account: str | None) -> None:
    host = select_host(settings(), LINUX.name, account)
    assert host == replace(LINUX, account=account or "agent")


@pytest.mark.parametrize(
    ("host", "account", "message"),
    [
        ("other", None, "host 'other' isn't configured"),
        (
            LINUX.name,
            "other",
            f"account 'other' isn't one of the accounts of {LINUX.name}",
        ),
    ],
)
def test_bad_host(host: str, account: str | None, message: str) -> None:
    with pytest.raises(Invalid) as caught:
        select_host(settings(), host, account)
    assert str(caught.value) == message
