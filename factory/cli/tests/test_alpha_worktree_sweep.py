"""Tests for the worktree sweep's helper and logging contracts."""

from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import worktree_sweep

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO


@pytest.mark.parametrize("removed", [True, False])
def test_sweep(
    removed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state") if removed else "")
    monkeypatch.setattr(worktree_sweep, "time", lambda: 0)
    root = tmp_path / ("state" if removed else ".local/state")
    directory = root / "tbhb/2026-11-13-tools-worktree-sweep/19700101T000000Z"
    libexec = tmp_path / ".local/libexec/tbhb"
    helpers = iter(
        [
            (libexec / "2026-11-13-tools-worktree-classify-all.sh", directory),
            (
                libexec / "2026-11-13-tools-worktree-apply.sh",
                directory / "plan.tsv",
                directory / "apply.log",
            ),
        ]
    )

    def run(
        argv: tuple[Path, ...], *, stdout: BinaryIO, env: dict[str, str], **_: object
    ) -> None:
        assert argv == next(helpers)
        expected = f"/opt/homebrew/bin:/usr/local/bin:{tmp_path}/.local/bin"
        expected += ":/usr/bin:/bin"
        assert env["PATH"] == expected
        if len(argv) == 2:
            (directory / "plan.tsv").write_text("REMOVE\n" if removed else "")
            stdout.write(b"classified\n")
        else:
            assert argv[1].read_text() == ("REMOVE\n" if removed else "")
            argv[2].write_text("applied\n")
            stdout.write(b"removed tree\n" if removed else b"")

    monkeypatch.setattr(worktree_sweep.subprocess, "run", run)
    assert alpha.main(["worktree-sweep"], "tbhbdev alpha") == 0
    expected = "removed tree\n" if removed else ""
    assert capsys.readouterr() == (expected, "")
    assert (directory / "classify.log").read_text() == "classified\n"
    assert (directory / "apply.log").read_text() == "applied\n"
    assert (directory / "apply.out").read_text() == expected


def test_missing_classifier_still_runs_apply(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Path, ...]] = []

    def run(argv: tuple[Path, ...], **_: object) -> None:
        calls.append(argv)
        if len(calls) == 1:
            message = "No such file or directory"
            raise FileNotFoundError(2, message, argv[0])

    monkeypatch.setattr(worktree_sweep.subprocess, "run", run)
    assert alpha.main(["worktree-sweep"], "tbhbdev alpha") == 0
    classifier, apply = calls
    assert apply[1] == classifier[1] / "plan.tsv"
    assert classifier[0].is_relative_to(isolated_home)
    log = classifier[1] / "classify.log"
    assert log.read_text() == f"{classifier[0]}: No such file or directory\n"
    assert capsys.readouterr() == ("", "")
