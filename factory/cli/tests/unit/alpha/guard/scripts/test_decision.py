# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise script placement decisions with supplied file state."""

from tbhbdev.alpha.guard.scripts.decision import refusal, under, valid_name
from tbhbdev.alpha.guard.scripts.paths import Place

NEW = "2026-11-09-coord-foo.sh"


def test_valid_name_unit() -> None:
    assert valid_name(NEW)
    assert valid_name("2024-02-29-a-b.v1.sh")
    assert not valid_name("2025-02-29-a-b.sh")
    assert not valid_name("2026-11-09-ab.sh")
    assert not valid_name("2026-11-31-a-b.sh")
    assert not valid_name("2026-12-01-a-b.sh\n")


def test_under() -> None:
    assert under("/a/b", "/a")
    assert under("/a", "/a")
    assert not under("/ab", "/a")
    assert not under("/a", "/a/b")
    assert under("/a/b", "/a/")
    assert not under("/", "/a")
    assert under("/a", "/")
    assert under("/aX/b", "/aX")
    assert not under("/a/b", "/aX")


def test_script_policy_uses_supplied_file_state() -> None:
    place = Place("/example/work", "/example", None)
    work = place.write("tool.sh")
    assert refusal(work, place, exists=False, names=["repo"]) is None
    assert refusal(work, place, exists=False, names=["work", "drafts"]) is not None

    handoff = place.write("~/.local/libexec/tbhb/2026-11-11-owner-tool.py")
    assert refusal(handoff, place, exists=False, names=None) is None
    assert refusal(handoff, place, exists=True, names=None) is not None
