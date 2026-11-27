"""Thin local Time Machine snapshots as a standalone macOS daemon."""

import os
import re
import subprocess  # nosec B404 - runs macOS tools by absolute path.
import time
from datetime import datetime

TMUTIL = "/usr/bin/tmutil"
SNAPSHOT_DATE = "%Y-%m-%d-%H%M%S"
MAX_AGE_SECONDS = 6 * 60 * 60
MIN_FREE_PERCENT = 15
UNKNOWN = ("unknown", "unknown", "unknown")


def command(*argv: str) -> str:
    return subprocess.run(  # nosec B603 - fixed tools with validated snapshot dates.
        argv,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


def snapshot_dates() -> list[str]:
    return sorted(
        line
        for line in command(TMUTIL, "listlocalsnapshotdates", "/").splitlines()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}", line)
    )


def free_space() -> tuple[int, float]:
    # The startup volume's writable Data volume shares its APFS container.
    fields = command("/bin/df", "-kP", "/System/Volumes/Data").splitlines()[1].split()
    total, available = int(fields[1]), int(fields[3])
    return available * 1024, 100 * available / total


def thin(dates: list[str]) -> tuple[int, float]:
    cutoff = time.time() - MAX_AGE_SECONDS
    recent = []
    for date in dates:
        # tmutil prints local wall time. timestamp() interprets it in the host's zone.
        if datetime.strptime(date, SNAPSHOT_DATE).timestamp() < cutoff:  # noqa: DTZ007
            command(TMUTIL, "deletelocalsnapshots", date)
        else:
            recent.append(date)

    available, percent = free_space()
    if percent < MIN_FREE_PERCENT:
        for date in recent:
            command(TMUTIL, "deletelocalsnapshots", date)
            available, percent = free_space()
            if percent > MIN_FREE_PERCENT:
                break
    return available, percent


def main() -> int:
    before: tuple[str, str, str] = UNKNOWN
    after: tuple[str, str, str] = UNKNOWN
    status = "ok"
    result = 0
    try:
        dates = snapshot_dates()
        available, percent = free_space()
        before = (str(len(dates)), str(available), f"{percent:.2f}")
        available, percent = thin(dates)
        after = (str(len(snapshot_dates())), str(available), f"{percent:.2f}")
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError) as error:
        status = repr(error)
        result = 1
    print(
        f"mac-snapshot-thin: snapshots_before={before[0]} snapshots_after={after[0]} "
        f"free_bytes_before={before[1]} free_bytes_after={after[1]} "
        f"free_percent_before={before[2]} free_percent_after={after[2]} status={status}"
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
