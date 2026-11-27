# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Isolate identity tests that exercise Git, hooks and harness state."""

import pytest
from unit.alpha.identity.identity_fixtures import (
    fake_bin,
    isolated,
    launch_file,
    plain_checkout,
    repo,
    wired,
)

__all__ = ["fake_bin", "isolated", "launch_file", "plain_checkout", "repo", "wired"]


@pytest.fixture(autouse=True)
def isolate_hook_tests(request: pytest.FixtureRequest) -> None:
    """Give I/O tests a temporary checkout and state directory."""
    if request.path.name.endswith("_io.py"):
        request.getfixturevalue("isolated")
