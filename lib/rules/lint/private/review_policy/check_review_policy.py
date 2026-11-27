# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# ruff: noqa: INP001
"""Run the pinned `tbhbdev alpha review policy check` and report its verdict.

Usage: check_review_policy.py TBHBDEV_SRC PATH

TBHBDEV_SRC is the `src` directory of the tbhbdev source tree, put on the
import path. The check exits 0 when clean, 1 with findings and 2 when PATH or
the floor fails to load.
"""

import os
import subprocess  # nosec B404
import sys


def main(argv: list[str]) -> int:
    source, path = argv
    python_path = [source, *filter(None, [os.environ.get("PYTHONPATH")])]
    command = [sys.executable, "-m", "tbhbdev", "alpha", "review", "policy", "check"]
    result = subprocess.run(  # noqa: S603  # nosec B603
        [*command, path],
        check=False,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(python_path)},
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
