import os
import runpy
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tbhbops import agent_scratch_reaper as reaper


@pytest.mark.parametrize("live", [False, True])
def test_tmp(
    live: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    names = ["old", "new", ".hidden", "tmux-x", "ssh-x", "systemd-x", "open", "foreign"]
    for name in names:
        (tmp_path / name).write_text("data")
        os.utime(tmp_path / name, (1, 1))
    (tmp_path / "new").touch()
    directory = tmp_path / "tree"
    directory.mkdir()
    (directory / "child").write_text("data")
    for path in (directory / "child", directory):
        os.utime(path, (1, 1))
    outside = tmp_path.parent / "outside"
    outside.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(outside)
    os.utime(link, (1, 1), follow_symlinks=False)
    original = Path.lstat

    def owner(path: Path) -> os.stat_result:
        value = original(path)
        if path.name == "foreign":
            fields = list(value)
            fields[4] = tmp_path.stat().st_uid + 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, "lstat", owner)
    assert (
        reaper.reap(
            tmp_path,
            bazel=False,
            active=[tmp_path / "open"],
            cutoff=2,
            deadline=float("inf"),
            live=live,
        )
        == 0
    )
    for name in ("old", "tree", "link"):
        assert (tmp_path / name).exists() is not live
    assert outside.read_text() == "preserve"
    for name in names[1:]:
        assert (tmp_path / name).exists()
    output = capsys.readouterr().out
    assert f"{'REMOVE' if live else 'DRYRUN'} {tmp_path / 'old'}" in output
    assert f"SKIP-IN-USE {tmp_path / 'new'}" in output


def test_idle(tmp_path: Path) -> None:
    (tmp_path / "new").touch()
    os.utime(tmp_path, (1, 1))
    device = tmp_path.stat().st_dev
    assert not reaper.idle(tmp_path, 2, device)
    assert not reaper.idle(tmp_path, float("inf"), device + 1)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize(
    "state", ["missing", "invalid", "zero", "gone", "denied", "live", "other"]
)
def test_server(
    platform: str, state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / "server"
    server.mkdir()
    if state != "missing":
        (server / "server.pid.txt").write_text(
            {"invalid": "bad", "zero": "0"}.get(state, "42")
        )
    monkeypatch.setattr(sys, "platform", platform)
    kill = Mock(
        side_effect={"gone": ProcessLookupError, "denied": PermissionError}.get(state)
    )
    monkeypatch.setattr(os, "kill", kill)
    command = "other" if state == "other" else "bazel(agent-e1)"
    run = Mock(return_value=SimpleNamespace(stdout=command))
    monkeypatch.setattr(subprocess, "run", run)
    original = Path.read_text

    def read(path: Path) -> str:
        return command if path == Path("/proc/42/cmdline") else original(path)

    monkeypatch.setattr(Path, "read_text", read)
    assert reaper.live_server(tmp_path) is (state in {"denied", "live"})


@pytest.mark.parametrize("mode", ["dry", "live", "error", "timeout", "scan-timeout"])
def test_bazel(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("a" * 32, "B" * 32, "install", "cache", "g" * 32):
        (tmp_path / name).mkdir()
        os.utime(tmp_path / name, (1, 1))
    (tmp_path / ("c" * 32)).symlink_to(tmp_path / ("a" * 32))
    (tmp_path / ("d" * 32)).touch()

    def server(path: Path) -> bool:
        return path.name == "B" * 32

    monkeypatch.setattr(reaper, "live_server", server)
    original_iterdir = Path.iterdir

    def entries(path: Path) -> list[Path]:
        return sorted(original_iterdir(path), key=lambda q: q.name != "a" * 32)

    monkeypatch.setattr(Path, "iterdir", entries)
    ticks = [0, 2] if mode == "scan-timeout" else [int(mode == "timeout")] * 20
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=ticks))
    if mode == "error":
        monkeypatch.setattr(
            reaper.shutil, "rmtree", Mock(side_effect=OSError("delete failed"))
        )
    assert reaper.reap(
        tmp_path, bazel=True, active=[], cutoff=2, deadline=1, live=mode != "dry"
    ) == (mode == "error")
    assert (tmp_path / ("a" * 32)).exists() is (mode != "live")
    assert (tmp_path / ("B" * 32)).exists()
    if "timeout" in mode:
        assert "max-seconds reached; stopping" in capsys.readouterr().out


def test_linux_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "self").mkdir()
    directory = tmp_path / "42"
    (directory / "fd").mkdir(parents=True)
    (directory / "cwd").symlink_to("/work")
    (directory / "fd/1").symlink_to("/work/file")
    (tmp_path / "43").symlink_to(tmp_path / "absent")
    assert reaper.process_paths(tmp_path.stat().st_uid, tmp_path) == [
        Path("/work"),
        Path("/work/file"),
    ]
    assert reaper.process_paths(tmp_path.stat().st_uid + 1, tmp_path) == []
    (directory / "cwd").unlink()
    assert reaper.process_paths(tmp_path.stat().st_uid, tmp_path) == [
        Path("/work/file")
    ]
    monkeypatch.setattr(Path, "readlink", Mock(side_effect=PermissionError))
    with pytest.raises(PermissionError):
        reaper.process_paths(tmp_path.stat().st_uid, tmp_path)


@pytest.mark.parametrize(
    "error", [None, FileNotFoundError, subprocess.CalledProcessError]
)
def test_macos_processes(
    error: type[Exception] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    failure = (
        subprocess.CalledProcessError(1, "lsof")
        if error is subprocess.CalledProcessError
        else error
    )
    run = Mock(
        return_value=SimpleNamespace(stdout="p42\nn/work\nn/work/file\nnTCP\nf1\n"),
        side_effect=failure,
    )
    monkeypatch.setattr(subprocess, "run", run)
    if error:
        with pytest.raises(error):
            reaper.process_paths(42)
    else:
        assert reaper.process_paths(42) == [Path("/work"), Path("/work/file")]
    run.assert_called_once_with(
        ["lsof", "-u", "42", "-F", "n"], check=True, capture_output=True, text=True
    )


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_main(
    failed: bool,
    explicit: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(reaper.getpass, "getuser", lambda: "agent")
    base = tmp_path / ".cache/bazel/_bazel_agent"
    base.mkdir(parents=True)
    active = [tmp_path / "busy/file"]
    monkeypatch.setattr(
        reaper,
        "process_paths",
        Mock(return_value=active, side_effect=PermissionError if failed else None),
    )
    reap = Mock(return_value=0)
    monkeypatch.setattr(reaper, "reap", reap)
    argv = ["--tmp-root", str(tmp_path)]
    if explicit:
        argv += ["--bazel-base", str(base), str(base)]
    assert reaper.main(argv) == failed
    assert reap.call_count == (2 if explicit else 1) + (not failed)
    assert capsys.readouterr().out.count("SPACE") == 4
    if not failed:
        assert reap.call_args_list[0].kwargs["active"] == active
        assert reap.call_args_list[0].kwargs["live"] is False


def test_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["reaper", "--tmp-root", str(tmp_path), "--bazel-base", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", [*argv, "--max-seconds", "0", "--live"])
    monkeypatch.setattr(
        subprocess, "run", Mock(return_value=SimpleNamespace(stdout=""))
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(reaper.__file__)), run_name="__main__")
    assert result.value.code == 0
