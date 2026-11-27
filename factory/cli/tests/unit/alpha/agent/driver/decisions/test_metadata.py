"""Check the wire shape and defaults of a local run record."""

import json

import pytest
from agent_example import LINUX, settings
from driver_run_example import BUILD

from tbhbdev.alpha.agent.driver.decisions.metadata import (
    Run,
    check_name,
    parse_record,
    record_fields,
)
from tbhbdev.alpha.agent.model import Invalid


@pytest.mark.parametrize("name", ["sample", "1", "A-z_0", "a" * 64])
def test_name(name: str) -> None:
    assert check_name(name) is None


@pytest.mark.parametrize("name", ["", "../bad", "a b", "a" * 65])
def test_bad_name(name: str) -> None:
    with pytest.raises(Invalid, match=r"run name .* must match"):
        check_name(name)


@pytest.mark.parametrize("older_record", [False, True])
def test_record(older_record: bool) -> None:
    expected = Run(
        "sample",
        LINUX,
        BUILD,
        "today",
        "owner",
    )
    expected.launched_by = ("tools", "session-1")
    expected.ignore_limits = True
    fields = record_fields(expected)
    assert fields["cleaning"] is False
    if older_record:
        fields["cleaning"] = True
        for name in ("launcher", "launcher_session", "ignore_limits"):
            del fields[name]
    saved = parse_record(settings(), "sample", json.dumps(fields))
    assert saved.host == expected.host
    assert saved.request == expected.request
    assert saved.started == expected.started
    assert saved.owner == expected.owner
    assert saved.launched_by == ((None, None) if older_record else expected.launched_by)
    assert saved.ignore_limits is (not older_record)
