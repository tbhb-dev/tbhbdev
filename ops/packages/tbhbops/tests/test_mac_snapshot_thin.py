import subprocess
from datetime import datetime
from unittest.mock import Mock, call

import pytest

from tbhbops import mac_snapshot_thin as thinner

LIST = ("/usr/bin/tmutil", "listlocalsnapshotdates", "/")
DELETE = ("/usr/bin/tmutil", "deletelocalsnapshots")
DF = ("/bin/df", "-kP", "/System/Volumes/Data")
HEADER = "Snapshot dates for disk /:\n"
OLD = "2026-11-25-055959"
EXACT = "2026-11-25-060000"
RECENT = "2026-11-25-090000"
NEWEST = "2026-11-25-110000"


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Mock:
    noon = datetime(2026, 11, 25, 12).timestamp()  # noqa: DTZ001
    monkeypatch.setattr(thinner.time, "time", lambda: noon)
    runner = Mock()
    monkeypatch.setattr(thinner.subprocess, "run", runner)
    return runner


def df(available: int) -> str:
    # APFS's Capacity column represents used / (used + available).
    return (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        f"/dev/disk3s5 20000 3000 {available} 50% /System/Volumes/Data\n"
    )


def respond(run: Mock, *outputs: str) -> None:
    run.side_effect = [
        subprocess.CompletedProcess([], 0, stdout=output) for output in outputs
    ]


def test_prune_by_age_and_recheck_space(
    run: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    respond(
        run, f"{HEADER}{EXACT}\n{OLD}\n", df(2000), "Deleted", df(3600), HEADER + EXACT
    )

    assert thinner.main() == 0
    assert [entry.args[0] for entry in run.call_args_list] == [
        LIST,
        DF,
        (*DELETE, OLD),
        DF,
        LIST,
    ]
    assert run.call_args_list[0].kwargs == {
        "check": True,
        "capture_output": True,
        "text": True,
        "env": {**thinner.os.environ, "LC_ALL": "C"},
    }
    assert capsys.readouterr().out == (
        "mac-snapshot-thin: snapshots_before=2 snapshots_after=1 "
        "free_bytes_before=2048000 free_bytes_after=3686400 "
        "free_percent_before=10.00 free_percent_after=18.00 status=ok\n"
    )


def test_space_pressure_deletes_oldest_first_until_above_target(
    run: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    respond(
        run,
        f"{HEADER}{RECENT}\n{NEWEST}\n{EXACT}\n",
        df(2500),
        df(2900),
        "Deleted",
        df(3000),
        "Deleted",
        df(3001),
        HEADER + NEWEST,
    )

    assert thinner.main() == 0
    assert [entry.args[0] for entry in run.call_args_list] == [
        LIST,
        DF,
        DF,
        (*DELETE, EXACT),
        DF,
        (*DELETE, RECENT),
        DF,
        LIST,
    ]
    assert "snapshots_before=3 snapshots_after=1" in capsys.readouterr().out


def test_no_snapshots_is_safe_even_under_pressure(
    run: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    respond(run, HEADER, df(2000), df(2000), HEADER)
    assert thinner.main() == 0
    assert [entry.args[0] for entry in run.call_args_list] == [LIST, DF, DF, LIST]
    assert "snapshots_before=0 snapshots_after=0" in capsys.readouterr().out


def test_exactly_fifteen_percent_does_not_trigger_thinning(run: Mock) -> None:
    respond(run, HEADER + RECENT, df(3000), df(3000), HEADER + RECENT)
    assert thinner.main() == 0
    assert [entry.args[0] for entry in run.call_args_list] == [LIST, DF, DF, LIST]


def test_failed_probe_emits_one_summary_line(
    run: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    run.side_effect = subprocess.CalledProcessError(1, LIST)
    assert thinner.main() == 1
    assert run.call_args_list == [
        call(
            LIST,
            check=True,
            capture_output=True,
            text=True,
            env={**thinner.os.environ, "LC_ALL": "C"},
        )
    ]
    [line] = capsys.readouterr().out.splitlines()
    assert line.startswith(
        "mac-snapshot-thin: snapshots_before=unknown snapshots_after=unknown "
        "free_bytes_before=unknown free_bytes_after=unknown "
        "free_percent_before=unknown free_percent_after=unknown "
        "status=CalledProcessError"
    )
