"""Host admission uses actual leases under an isolated state directory."""

from __future__ import annotations

import errno
import os
import pwd
import stat
import time
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agent_example import LINUX

from tbhbdev.alpha.agent import admission
from tbhbdev.alpha.agent import command as agent
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from pathlib import Path


def test_builds_leave_one_slot_for_reviews(tmp_path: Path) -> None:
    host = replace(LINUX, max_concurrent_runs=3)
    alternate_account = replace(host, account=host.accounts[-1])
    root = tmp_path / "admission"
    with ExitStack() as slots:
        for _ in range(2):
            slots.enter_context(
                admission.reserve(root, host, Profile.BUILD, host.max_concurrent_runs)
            )
        with (
            pytest.raises(Invalid, match="reserved for reviews"),
            admission.reserve(root, host, Profile.BUILD, host.max_concurrent_runs),
        ):
            pytest.fail("build claimed the review slot")
        with (
            admission.reserve(
                root,
                alternate_account,
                Profile.REVIEW,
                alternate_account.max_concurrent_runs,
            ),
            pytest.raises(Invalid, match="max_concurrent_runs=3"),
            admission.reserve(root, host, Profile.REVIEW, host.max_concurrent_runs),
        ):
            pytest.fail("review exceeded the host limit")
        with admission.reserve(root, host, Profile.REVIEW, host.max_concurrent_runs):
            assert not (root / "review-1").exists()
    with admission.reserve(root, host, Profile.BUILD, host.max_concurrent_runs):
        assert not (root / "build-2").exists()


def test_reviews_can_use_shared_capacity(tmp_path: Path) -> None:
    host = replace(LINUX, max_concurrent_runs=2)
    root = tmp_path / "admission"
    with (
        admission.reserve(root, host, Profile.REVIEW, host.max_concurrent_runs),
        admission.reserve(root, host, Profile.REVIEW, host.max_concurrent_runs),
    ):
        assert (root / "review-1").exists()


@pytest.mark.parametrize("profile", list(Profile))
def test_zero_slots_disables_every_profile(tmp_path: Path, profile: Profile) -> None:
    host = replace(LINUX, max_concurrent_runs=0)
    with (
        pytest.raises(Invalid, match="max_concurrent_runs=0"),
        admission.reserve(
            tmp_path / "admission", host, profile, host.max_concurrent_runs
        ),
    ):
        pytest.fail("disabled host admitted a run")


def test_a_sole_slot_admits_a_build_then_refuses(tmp_path: Path) -> None:
    host = replace(LINUX, max_concurrent_runs=1)
    root = tmp_path / "admission"
    with (
        admission.reserve(root, host, Profile.BUILD, host.max_concurrent_runs),
        pytest.raises(Invalid, match="max_concurrent_runs=1"),
        admission.reserve(root, host, Profile.REVIEW, host.max_concurrent_runs),
    ):
        pytest.fail("a second run took the only slot")


def test_widens_a_directory_this_account_owns(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    root.mkdir(mode=0o755)
    with admission.reserve(root, LINUX, Profile.REVIEW, LINUX.max_concurrent_runs):
        assert stat.S_IMODE(root.stat().st_mode) == 0o1777


def test_refuses_a_directory_with_the_wrong_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = os.getuid() + 1
    monkeypatch.setattr(admission.os, "getuid", lambda: other)
    with (
        pytest.raises(Invalid, match="directory must have mode 1777"),
        admission.reserve(tmp_path, LINUX, Profile.REVIEW, LINUX.max_concurrent_runs),
    ):
        pytest.fail("admitted a run in a directory with the wrong mode")


def test_refuses_a_slot_that_is_not_regular(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    with admission.reserve(root, LINUX, Profile.REVIEW, LINUX.max_concurrent_runs):
        (root / "review-1").mkdir()
        with (
            pytest.raises(Invalid, match="slot must be a regular file"),
            admission.reserve(root, LINUX, Profile.REVIEW, LINUX.max_concurrent_runs),
        ):
            pytest.fail("admitted a run with an invalid slot")


def test_refuses_a_symlink_slot(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.touch()
    (tmp_path / "review-0").symlink_to(target)
    with (
        pytest.raises(OSError, check=lambda error: error.errno == errno.ELOOP),
        admission.reserve(tmp_path, LINUX, Profile.REVIEW, LINUX.max_concurrent_runs),
    ):
        pytest.fail("admitted a run with a symlink slot")


@pytest.mark.parametrize("name", ["admission.lock", "review-0"])
@pytest.mark.parametrize("owner", ["root", "configured", "unrelated"])
def test_admission_file_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, owner: str
) -> None:
    slot = tmp_path / name
    slot.touch()
    inode = slot.stat().st_ino
    uid = os.getuid()
    owner_uid = {"root": 0, "configured": uid + 1, "unrelated": uid + 2}[owner]

    def getpwnam(account: str) -> SimpleNamespace:
        if account == LINUX.accounts[-1]:
            return SimpleNamespace(pw_uid=uid + 1)
        raise KeyError(account)

    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result | SimpleNamespace:
        info = real_fstat(fd)
        if info.st_ino == inode:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=owner_uid)
        return info

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(admission.os, "fstat", fstat)
    expectation = (
        pytest.raises(Invalid, match="slot must be owned by")
        if owner == "unrelated"
        else nullcontext()
    )
    with (
        expectation,
        admission.reserve(tmp_path, LINUX, Profile.REVIEW, LINUX.max_concurrent_runs),
    ):
        assert (tmp_path / "review-0").is_file()
    monkeypatch.setattr(admission, "ROOT", tmp_path)
    probe = [
        "check-slot",
        LINUX.name,
        "review",
        str(LINUX.max_concurrent_runs),
        "--",
        *LINUX.accounts,
    ]
    assert agent.main(probe, "example agent") == (1 if owner == "unrelated" else 0)


@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_refuses_a_group_or_other_writable_slot(tmp_path: Path, mode: int) -> None:
    slot = tmp_path / "review-0"
    slot.touch()
    slot.chmod(mode)
    with (
        pytest.raises(Invalid, match="slot must not be group- or other-writable"),
        admission.reserve(tmp_path, LINUX, Profile.REVIEW, LINUX.max_concurrent_runs),
    ):
        pytest.fail("admitted a run with a group- or other-writable slot")


def test_a_request_without_a_cap_is_refused(tmp_path: Path) -> None:
    with (
        pytest.raises(Invalid, match="carries no max_concurrent_runs"),
        admission.reserve(tmp_path / "admission", LINUX, Profile.REVIEW, None),
    ):
        pytest.fail("no slot may be claimed")


def test_the_request_cap_overrides_the_host_default(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    host = replace(LINUX, max_concurrent_runs=3)
    with (
        admission.reserve(root, host, Profile.REVIEW, 1),
        pytest.raises(Invalid, match="max_concurrent_runs=1"),
        admission.reserve(root, host, Profile.REVIEW, 1),
    ):
        pytest.fail("the second slot must be refused")


def test_check_slot_respects_the_review_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "admission"
    monkeypatch.setattr(admission, "ROOT", root)
    build = ["check-slot", LINUX.name, "build", "2"]
    review = ["check-slot", LINUX.name, "review", "2"]
    assert agent.main(build, "example agent") == 0
    assert capsys.readouterr().out == ""
    assert not (root / "build-0").exists()
    with admission.reserve(root, LINUX, Profile.BUILD, 2):
        assert agent.main(build, "example agent") == 0
        assert capsys.readouterr().out == "full\n"
        assert agent.main(review, "example agent") == 0
        assert capsys.readouterr().out == ""
        assert not (root / "review-0").exists()
        with admission.reserve(root, LINUX, Profile.REVIEW, 2):
            assert agent.main(review, "example agent") == 0
            assert capsys.readouterr().out == "full\n"


def test_check_slot_reports_a_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "admission"
    root.touch()
    monkeypatch.setattr(admission, "ROOT", root)
    assert agent.main(["check-slot", LINUX.name, "build", "2"], "example agent") == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "example agent check-slot:" in output.err


@pytest.mark.parametrize(("args", "rc"), [(["--help"], 0), ([], 2)])
def test_check_slot_validates_arguments(args: list[str], rc: int) -> None:
    assert agent.main(["check-slot", *args], "example agent") == rc


def test_admitted_counts_held_builds_and_reviews_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "admission"
    monkeypatch.setattr(admission, "ROOT", root)
    command = ["admitted", "--", *LINUX.accounts]
    assert agent.main(command, "example agent") == 0
    assert capsys.readouterr().out == "0 0\n"
    with admission.reserve(root, LINUX, Profile.BUILD, 3):
        assert agent.main(command, "example agent") == 0
        assert capsys.readouterr().out == "1 1\n"
        with admission.reserve(root, LINUX, Profile.REVIEW, 3):
            assert agent.main(command, "example agent") == 0
            assert capsys.readouterr().out == "2 2\n"
    assert agent.main(command, "example agent") == 0
    assert capsys.readouterr().out == "0 0\n"


def test_admitted_reports_a_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "admission"
    root.touch()
    monkeypatch.setattr(admission, "ROOT", root)
    assert agent.main(["admitted", "--", *LINUX.accounts], "example agent") == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "example agent admitted:" in output.err
    assert agent.main(["admitted", "--help"], "example agent") == 0


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_reserve_stamps_the_slot_with_the_claim_time(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    root.mkdir(mode=0o1777)
    stale = root / "build-0"
    stale.touch()
    _age(stale, 10_000)
    before = time.time()
    with admission.reserve(root, LINUX, Profile.BUILD, 3):
        assert stale.stat().st_mtime >= before - 1
        assert time.time() - stale.stat().st_mtime < admission.RECENT_SECONDS


def test_active_count_separates_recent_from_older_runs(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    accounts = LINUX.accounts
    assert admission.active_count(root, accounts) == (0, 0)
    with ExitStack() as slots:
        for _ in range(3):
            slots.enter_context(admission.reserve(root, LINUX, Profile.BUILD, 5))
        slots.enter_context(admission.reserve(root, LINUX, Profile.REVIEW, 5))
        assert admission.active_count(root, accounts) == (4, 4)
        _age(root / "build-0", admission.RECENT_SECONDS - 1)
        assert admission.active_count(root, accounts) == (4, 4)
        _age(root / "build-0", admission.RECENT_SECONDS + 1)
        assert admission.active_count(root, accounts) == (4, 3)
        _age(root / "build-1", 3600)
        _age(root / "review-0", 3600)
        assert admission.active_count(root, accounts) == (4, 1)
    assert admission.active_count(root, accounts) == (0, 0)


def test_a_slot_that_is_not_held_is_neither_total_nor_recent(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    with admission.reserve(root, LINUX, Profile.BUILD, 3):
        pass
    assert (root / "build-0").exists()
    assert admission.active_count(root, LINUX.accounts) == (0, 0)


def test_the_recent_window_is_five_minutes() -> None:
    assert admission.RECENT_SECONDS == 300
