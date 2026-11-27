# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for identity hook error reporting."""

from tbhbdev.alpha.identity.diagnostics import hook_failure


def test_hook_failure_exits_successfully_only_for_recording() -> None:
    assert hook_failure("record", "ValueError", "save", 12) == (
        "tbhbdev alpha identity record: failed: ValueError in save (line 12)",
        0,
    )
    assert hook_failure("check", "OSError", "open", 8)[1] == 1
