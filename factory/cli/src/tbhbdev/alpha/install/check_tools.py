"""Install the repository's check tools for the current account."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from tbhbdev.alpha import tool_pins
from tbhbdev.alpha.agent.host_tools import account_path
from tbhbdev.alpha.install.model import PROG, InstallError
from tbhbdev.alpha.repo import git, in_tbhb_dev


def install_check_tools(*, dry_run: bool) -> int:
    code, root = git("rev-parse", "--show-toplevel")
    if code or not in_tbhb_dev(Path(root)):
        return 0
    top = Path(root)
    home = Path.home()
    directory = home / ".local" / "bin"
    env = os.environ | {"PATH": os.pathsep.join(account_path(str(home)))}
    try:
        pins, problems = tool_pins.check_tools(top, env, home)
        if not problems:
            return 0
        specs = {key: f"{key}@{version}" for key, version in pins.items()}
        if dry_run:
            print(f"would install and link pinned check tools in {directory}")
            return 0
        install_and_link(top, directory, specs, tuple(problems))
    except (InstallError, OSError, TypeError, ValueError) as exc:
        print(f"{PROG}: could not install pinned check tools: {exc}", file=sys.stderr)
        return 1
    print(f"{PROG}: wrote pinned check tool links in {directory}")
    return 0


def install_and_link(
    top: Path, directory: Path, specs: dict[str, str], mismatches: tuple[str, ...]
) -> None:
    done = tool_pins.tool_command(
        ["mise", "install", *specs.values()], cwd=top, timeout=600
    )
    if done.returncode:
        message = "mise install failed. Check the repository's mise configuration."
        raise InstallError(message)
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=directory) as staging:
        for key in mismatches:
            name = tool_pins.CHECK_TOOLS[key]
            result = tool_pins.tool_command(
                ["mise", "which", name, f"--tool={specs[key]}"], cwd=top
            )
            source = Path(result.stdout.strip())
            if result.returncode or not source.is_absolute():
                message = f"mise could not locate {specs[key]}"
                raise InstallError(message)
            (Path(staging) / name).symlink_to(source)
        for link in Path(staging).iterdir():
            link.replace(directory / link.name)
