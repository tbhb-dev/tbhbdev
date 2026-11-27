"""Install and pin the verified Claude Code status line."""

import hashlib
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.install.files import read_config, write_config
from tbhbdev.alpha.install.model import CLAUDE, PROG, InstallError
from tbhbdev.alpha.install.settings import config_path, format_config
from tbhbdev.alpha.install.statusline.command import (
    missing_tool,
    pinned_settings,
    replaceable_line,
    statusline_command,
    statusline_package,
    statusline_script,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


STATUSLINE_VERSION = "2.2.33"
STATUSLINE_TIMEOUT = 300
STATUSLINE_MAX_BYTES = 32 * 1024 * 1024
STATUSLINE_CHUNK = 1024 * 1024
# SHA-256 of the pinned release's files as package_digest reads them.
STATUSLINE_DIGEST = "920e056560d0dd296363b145d96c91bf727934adf2831fd80647c55665478cd3"


def statusline_root() -> Path:
    """The directory the pinned ccstatusline is installed in."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "tbhbdev" / "ccstatusline" / STATUSLINE_VERSION


def package_digest(package: Path) -> str | None:
    """SHA-256 of every file's relative name and bytes, or None past a symlink.

    Files are read in chunks, and a package over STATUSLINE_MAX_BYTES gives None.

    The package's bin imports sibling chunks, so the digest covers the whole
    directory. A link or any entry that isn't a file or directory gives None.
    """
    digest = hashlib.sha256()
    remaining = STATUSLINE_MAX_BYTES
    for entry in sorted(package.rglob("*")):
        if entry.is_symlink():
            return None
        if entry.is_file():
            name = os.fsencode(entry.relative_to(package).as_posix())
            digest.update(name + b"\0")
            with entry.open("rb") as handle:
                while chunk := handle.read(STATUSLINE_CHUNK):
                    remaining -= len(chunk)
                    if remaining < 0:
                        return None
                    digest.update(chunk)
            digest.update(b"\0")
        elif not entry.is_dir():
            return None
    return digest.hexdigest()


def statusline_installed(root: Path) -> bool:
    """Whether ROOT is a complete, unaltered install of the pinned version.

    No path from ROOT down to the package is a symlink. The package files
    must hash to the pinned digest.
    """
    package = statusline_package(root)
    path = (root, root / "node_modules", package)
    try:
        return (
            not any(entry.is_symlink() for entry in path)
            and statusline_script(root).is_file()
            and package_digest(package) == STATUSLINE_DIGEST
        )
    except OSError:
        # An unreadable file is no install, and a rerun replaces it.
        return False


def add_statusline(root: Path, bun: str | None, run: Callable[..., object]) -> None:
    """Install the pinned ccstatusline in a private staging directory, then move it.

    Each run stages in a directory of its own, so concurrent installers can't
    promote each other's half-written files. ROOT appears only after bun exited
    cleanly and the staged package passed the same check a later run applies, so
    a bun that wrote files and then died or timed out leaves no install that a
    later run could trust. The staging directory goes on every outcome.
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.staging-", dir=root.parent))
    try:
        run(
            [
                bun,
                "add",
                "--exact",
                "--ignore-scripts",
                f"ccstatusline@{STATUSLINE_VERSION}",
            ],
            cwd=staging,
            check=True,
            capture_output=True,
            timeout=STATUSLINE_TIMEOUT,
        )
        if not statusline_installed(staging):
            message = f"ccstatusline {STATUSLINE_VERSION} in {staging} failed its check"
            raise InstallError(message)
        if root.is_symlink():
            root.unlink()
        else:
            shutil.rmtree(root, ignore_errors=True)
        staging.replace(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def pin_statusline(path: Path, install_dir: Path, wanted: str) -> Path | None:
    """Write WANTED into the config as it is now; None when it's no longer ours.

    The file is read again here, after the install, so an edit made while bun
    ran is kept.
    """
    old, config = read_config(path)
    merged = pinned_settings(config, install_dir, wanted)
    if merged is None:
        return None
    return write_config(path, old, format_config(merged))


def install_statusline(  # noqa: PLR0911 -- one early return per outcome
    env: Mapping[str, str],
    *,
    dry_run: bool,
    run: Callable[..., object] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    """Pin ccstatusline and point Claude's `statusLine.command` at it.

    Only the known `bunx -y ccstatusline@latest` command, and a command this
    installer wrote with another node path, are replaced. Any other command,
    and a config with no status line, stays as it is. Without bun (to install)
    or node (to run it) the old command stays and the reason is printed. The
    old file is backed up, only `statusLine.command` changes, the config is read
    again just before the write.
    """
    path = config_path(CLAUDE, env, Path.home())
    try:
        _, config = read_config(path)
    except InstallError as exc:
        print(f"{PROG}: claude: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    root = statusline_root()
    script = statusline_script(root)
    line = replaceable_line(config, root.parent)
    if line is None:
        return 0
    bun, node = which("bun"), which("node")
    installed = statusline_installed(root)
    missing = missing_tool(node, bun, installed=installed)
    if node is None or missing:
        print(
            f"claude: {missing} not found; left the statusLine command in {path}",
            file=sys.stderr,
        )
        return 0
    wanted = statusline_command(node, script)
    if line["command"] == wanted and installed:
        print(f"claude: statusLine in {path} is up to date")
        return 0
    if dry_run:
        print(f"claude: would install ccstatusline {STATUSLINE_VERSION} in {root}")
        return 0
    try:
        if not installed:
            add_statusline(root, bun, run)
        backup = pin_statusline(path, root.parent, wanted)
    except (OSError, subprocess.SubprocessError, InstallError) as exc:
        print(f"{PROG}: claude: cannot pin ccstatusline: {exc}", file=sys.stderr)
        return 1
    if backup is None:
        print(
            f"claude: statusLine in {path} changed while ccstatusline installed;"
            " left as it is",
            file=sys.stderr,
        )
        return 0
    print(
        f"claude: pinned ccstatusline {STATUSLINE_VERSION} in {path}; "
        f"the old one is {backup}"
    )
    return 0
