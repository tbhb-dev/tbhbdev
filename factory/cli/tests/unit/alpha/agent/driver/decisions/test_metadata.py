"""Check the wire shape and defaults of a local run record."""

import json

import pytest
from agent_example import LINUX, settings
from driver_run_example import BUILD

from tbhbdev.alpha.agent.driver.decisions.metadata import (
    Run,
    cleaning_record,
    parse_record,
    record_fields,
)


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
    if older_record:
        for name in ("launcher", "launcher_session", "ignore_limits"):
            del fields[name]
    saved = parse_record(settings(), "sample", cleaning_record(json.dumps(fields)))
    assert saved.host == expected.host
    assert saved.request == expected.request
    assert saved.started == expected.started
    assert saved.owner == expected.owner
    assert saved.cleaning
    assert saved.launched_by == ((None, None) if older_record else expected.launched_by)
    assert saved.ignore_limits is (not older_record)
