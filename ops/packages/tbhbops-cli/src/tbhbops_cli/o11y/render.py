"""Read configuration trees and write private, reproducible releases."""

import argparse
import os.path
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
lazy from collections.abc import Generator

from tbhbops.o11y import render

from tbhbops_cli.common import CliError
lazy from tbhbops_cli.o11y.filesystem import Filesystem

if TYPE_CHECKING:
    from tbhbops_cli.cli import System


def absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        message = "--config-root must be an absolute path"
        raise argparse.ArgumentTypeError(message)
    return path


@contextmanager
def _step(kind: str, path: Path) -> Generator[None]:
    try:
        yield
    except (OSError, ValueError) as exc:
        message = f"render: {kind} {path} failed: {type(exc).__name__}"
        raise CliError(message) from None


def _validate_source(fs: Filesystem, source: Path, directory: Path) -> None:
    for name in fs.list_dir(directory):
        path = directory / name
        info = fs.lstat(path)
        if info is None:
            raise FileNotFoundError
        relative = str(path.relative_to(source))
        if stat.S_ISLNK(info.st_mode):
            message = f"render: source symlink {relative!r} is not supported"
            raise CliError(message)
        if stat.S_ISDIR(info.st_mode):
            _validate_source(fs, source, path)
        elif name.startswith(".tmp."):
            message = f"render: source temporary file {relative!r} is not supported"
            raise CliError(message)


def _discard(fs: Filesystem, files: list[Path], directories: list[Path]) -> None:
    while files:
        path = files[-1]
        if fs.lstat(path) is not None:
            fs.unlink(path)
        files.pop()
    while directories:
        fs.rmdir(directories[-1])
        directories.pop()


def remove_tree(fs: Filesystem, path: Path) -> None:
    info = fs.lstat(path)
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode):
        raise OSError
    if stat.S_ISDIR(info.st_mode):
        for name in fs.list_dir(path):
            remove_tree(fs, path / name)
        fs.rmdir(path)
    else:
        fs.unlink(path)


def _substitute(content: bytes, settings: render.Settings) -> bytes:
    if b"@O11Y_" in content:
        return render.substitute(content.decode(), settings).encode()
    return content


def _copy_tree(
    fs: Filesystem,
    source: Path,
    target: Path,
    settings: render.Settings,
    written: tuple[list[Path], list[Path]],
) -> None:
    files, directories = written
    fs.mkdir(target)
    directories.append(target)
    for name in fs.list_dir(source):
        src, dst = source / name, target / name
        info = fs.lstat(src)
        if info is None:
            raise FileNotFoundError
        if stat.S_ISDIR(info.st_mode):
            _copy_tree(fs, src, dst, settings, written)
        elif stat.S_ISREG(info.st_mode):
            content = _substitute(fs.read_bytes(src), settings)
            mode = 0o755 if info.st_mode & 0o111 else 0o644
            files.append(dst)
            fs.write_atomic(dst, content, mode)
        else:
            message = "render source must contain only regular files and directories"
            raise CliError(message)
    fs.chmod(target, 0o755)  # nosec B103: containers must traverse config directories


def tree_files(fs: Filesystem, directory: Path, prefix: str = "") -> dict[str, bytes]:
    files = {}
    for name in fs.list_dir(directory):
        path = directory / name
        info = fs.lstat(path)
        if info is None:
            raise FileNotFoundError
        relative = prefix + name
        if stat.S_ISDIR(info.st_mode):
            files.update(tree_files(fs, path, relative + "/"))
        elif stat.S_ISREG(info.st_mode):
            files[relative] = fs.read_bytes(path)
    return files


def _render_config(
    fs: Filesystem, source: Path, config: Path, name: str, settings: render.Settings
) -> str:
    incoming = config / f".incoming-{name}"
    remove_tree(fs, incoming)
    files: list[Path] = []
    directories: list[Path] = []
    try:
        _copy_tree(
            fs, source / "config" / name, incoming, settings, (files, directories)
        )
        text = render.manifest(tree_files(fs, incoming))
        target = render.target_name(name, text)
        existing = fs.lstat(config / target)
        if existing is not None and stat.S_ISDIR(existing.st_mode):
            _discard(fs, files, directories)
        else:
            manifest_path = incoming / "MANIFEST.sha256"
            if manifest_path not in files:
                files.append(manifest_path)
            fs.write_atomic(manifest_path, text + "\n", 0o644)
            fs.rename(incoming, config / target)
    except BaseException:
        _discard(fs, files, directories)
        raise
    return target


def _release(
    args: argparse.Namespace, system: System, settings: render.Settings
) -> None:
    fs = system.filesystem
    source = system.cwd / args.source
    out = Path(os.path.normpath(system.cwd / args.out))
    with _step("ValidateSource", Path()):
        for name in ("compose.yaml", "compose.tools.yaml"):
            info = fs.lstat(source / name)
            if info is None or not stat.S_ISREG(info.st_mode):
                message = f"render: source is missing {name}"
                raise CliError(message)
        _validate_source(fs, source, source)
    with _step("PrepareOutput", Path()):
        out = fs.prepare_output(out)
    config = out / "config"
    with _step("EnsureDir", Path("config")):
        fs.mkdir(config)
    release = out / "release"
    remove_tree(fs, release)
    fs.mkdir(release)
    for name in ("compose.yaml", "compose.tools.yaml"):
        with _step("CopyCompose", Path("release") / name):
            fs.write_atomic(
                release / name,
                _substitute(fs.read_bytes(source / name), settings),
                0o600,
            )
    fs.write_atomic(
        release / "dummy-s3.env",
        "AWS_ACCESS_KEY_ID=dummy-not-a-secret\nAWS_SECRET_ACCESS_KEY=dummy-not-a-secret\n",
        0o600,
    )
    targets = {}
    with _step("ReadConfigs", Path("config")):
        names = fs.list_dir(source / "config")
    for name in names:
        optional_input = {
            "ts": "O11Y_TS_HOSTNAME",
            "grafana_tls": "O11Y_GRAFANA_TLS_CERT_DIR",
        }.get(name)
        if optional_input and not dict(settings.values).get(optional_input):
            continue
        with _step("RenderConfig", Path("config") / name):
            info = fs.lstat(source / "config" / name)
            if info is None or not stat.S_ISDIR(info.st_mode):
                continue
            targets[name] = _render_config(fs, source, config, name, settings)
    lines = render.env_lines(settings, targets, str(args.config_root or config))
    text = "\n".join(lines) + "\n"
    data = dict(settings.values)["O11Y_DATA_DIR"]
    fs.write_atomic(
        release / ".env", text + f"O11Y_S3_ENV_FILE={data}/secrets/s3.env\n", 0o600
    )
    fs.write_atomic(
        release / ".env.validate", text + "O11Y_S3_ENV_FILE=dummy-s3.env\n", 0o600
    )
    fs.write_atomic(
        release / "MANIFEST.sha256",
        render.manifest(tree_files(fs, release)) + "\n",
        0o600,
    )


def dispatch(args: argparse.Namespace, system: System) -> None:
    """Report validation and I/O categories without environment values."""
    settings = render.check(system.env)
    if isinstance(settings, render.Errors):
        raise CliError("\n".join(settings.messages))
    with _step("WriteRelease", Path("release")):
        _release(args, system, settings)
