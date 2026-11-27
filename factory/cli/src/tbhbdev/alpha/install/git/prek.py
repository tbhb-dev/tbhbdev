"""Find and run prek for Git hook installation."""

import shutil
import subprocess  # nosec B404
import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha.install.model import InstallError

if TYPE_CHECKING:
    from pathlib import Path


def find_prek() -> list[str]:
    """How to run prek: from PATH, else through mise."""
    # On a case-insensitive filesystem, as macOS has by default, which("PREK")
    # finds prek too, so the two `which` lines are exempt from mutation.
    if shutil.which("prek"):  # pragma: no mutate
        return ["prek"]
    if shutil.which("mise"):  # pragma: no mutate
        return ["mise", "exec", "--", "prek"]
    message = "prek not found; install it, or install mise"
    raise InstallError(message)


def prek_run(
    prek: list[str], top: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run prek in the work tree, its output passed through."""
    # check=False is subprocess.run's default, so dropping it changes nothing.
    return subprocess.run(  # nosec B603
        [*prek, *args],
        cwd=top,
        check=False,  # pragma: no mutate
        text=True,
        capture_output=True,
    )


def prek_install(prek: list[str], top: Path, *args: str) -> None:
    """Run `prek install ARGS`; stop on failure."""
    done = prek_run(prek, top, "install", *args)
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    if done.returncode != 0:
        message = f"prek install {' '.join(args)} failed"
        raise InstallError(message)
