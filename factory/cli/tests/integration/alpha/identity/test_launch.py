# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity launch tests."""

import json
from typing import TYPE_CHECKING

import pytest
from integration.alpha.identity.identity_test_support import (
    CHILD,
    EARLY,
    LATE,
    LAUNCH,
    NOW_UTC,
    OTHER,
    ROOT,
    Json,
    loaded,
)

from tbhbdev.alpha.identity import launch, sessions

if TYPE_CHECKING:
    from pathlib import Path


def test_claim_launch_goes_to_the_first_root_session(launch_file: Path) -> None:
    fields = {"launch": LAUNCH, "started_at": EARLY}
    assert launch.claim_launch(ROOT) == fields
    assert loaded(launch_file) == {**fields, "session": ROOT}
    assert not list(launch_file.parent.glob("*.tmp"))
    assert launch.claim_launch(OTHER) == {}
    assert launch.claim_launch(ROOT) == fields
    assert loaded(launch_file) == {**fields, "session": ROOT}


@pytest.mark.parametrize(
    "content",
    [
        {"launch": LAUNCH},
        {"started_at": EARLY},
        {"launch": "nope", "started_at": EARLY},
        {"launch": LAUNCH, "started_at": "2026-02-30T00:00:00Z"},
    ],
)
def test_claim_launch_leaves_an_invalid_file_alone(
    launch_file: Path, content: Json
) -> None:
    launch_file.write_text(json.dumps(content))
    assert launch.claim_launch(ROOT) == {}
    assert loaded(launch_file) == content


def test_claim_launch_without_a_file_or_a_repository(repo: Path, plain: Path) -> None:
    assert launch.claim_launch(ROOT) == {}
    assert launch.claim_launch(ROOT, plain) == {}
    assert launch.claim_launch(ROOT, repo) == {}
    assert not (repo / ".git" / "tbhb-alpha" / "launch.json").exists()


@pytest.mark.usefixtures("clock")
def test_start_fields_for_a_root_and_a_subagent(launch_file: Path) -> None:
    fields: dict[str, object] = {"launch": LAUNCH, "started_at": EARLY}
    assert launch.start_fields(ROOT, ROOT) == fields
    sessions.write_record(ROOT, {}, once=fields)
    assert launch.start_fields(ROOT, ROOT) == {}
    # A later session finds the file claimed and starts at its own first event.
    assert launch.start_fields(OTHER, OTHER) == {"started_at": NOW_UTC}
    assert launch.start_fields(CHILD, ROOT) == {"started_at": NOW_UTC, "launch": LAUNCH}
    assert launch.start_fields(CHILD, OTHER) == {"started_at": NOW_UTC, "launch": None}
    sessions.write_record(CHILD, {"root": ROOT}, once={"started_at": LATE})
    assert launch.start_fields(CHILD, ROOT) == {}
    assert loaded(launch_file)["session"] == ROOT


@pytest.mark.usefixtures("clock")
def test_start_fields_claims_the_launch_for_a_root_that_began_elsewhere(
    launch_file: Path,
) -> None:
    sessions.write_record(ROOT, {}, once={"started_at": LATE})
    assert launch.start_fields(ROOT, ROOT) == {"launch": LAUNCH}
    assert loaded(launch_file)["session"] == ROOT
    # A subagent or a root that has its launch asks for nothing more.
    sessions.write_record(CHILD, {"root": ROOT}, once={"started_at": LATE})
    assert launch.start_fields(CHILD, ROOT) == {}
    sessions.write_record(ROOT, {}, once={"launch": LAUNCH})
    assert launch.start_fields(ROOT, ROOT) == {}
    # A start that is not a valid time counts as no start.
    sessions.write_record(OTHER, {}, once={"started_at": "0"})
    assert launch.start_fields(OTHER, OTHER) == {"started_at": NOW_UTC}
