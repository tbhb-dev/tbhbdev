"""Prepare observability data directories and secrets."""

import argparse
import stat
from typing import TYPE_CHECKING, TextIO, assert_never
lazy from pathlib import Path

from tbhbops.o11y import init
from tbhbops_cli.common import CliError
lazy from tbhbops_cli.o11y.filesystem import Filesystem

if TYPE_CHECKING:
    from collections.abc import Callable

    from tbhbops_cli.cli import System


def owner(value: str) -> tuple[int, int]:
    """Parse a numeric uid:gid pair as a usage argument."""
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isascii() and p.isdecimal() for p in parts):  # noqa: PLR2004  # uid and gid
        msg = "expected UID:GID"
        raise argparse.ArgumentTypeError(msg)
    return int(parts[0]), int(parts[1])


class PlanIncompleteError(Exception):
    """A fact could not be inspected safely."""


def _inspect[T](call: Callable[[Path], T], path: Path, data: Path, euid: int) -> T:
    try:
        return call(path)
    except PermissionError:
        if euid == 0:
            raise
        relative = path.relative_to(data, walk_up=True)
        msg = f"init: can't inspect {str(relative)!r}; plan incomplete"
        raise PlanIncompleteError(msg) from None


def _symlinks(fs: Filesystem, data: Path, euid: int) -> tuple[str, ...]:
    paths = {data, *data.parents}
    names = [s.name for s in init.SECRETS] + [init.WEBHOOK, init.SMTP, "s3.env"]
    paths.update(data / d.name for d in init.DIRECTORIES)
    paths.update(data / s.guard for s in init.SECRETS if s.guard is not None)
    for name in names:
        paths.add(data / "secrets" / name)
    # Parents are checked first so no later read traverses a known symlink.
    for path in sorted(paths):
        info = _inspect(fs.lstat, path, data, euid)
        if info is not None and stat.S_ISLNK(info.st_mode):
            return (str(path),)
    return ()


def _leftovers(
    fs: Filesystem, directory: Path, euid: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    leftovers = []
    links = []
    if _inspect(fs.lstat, directory, directory.parent, euid) is not None:
        for name in _inspect(fs.list_dir, directory, directory.parent, euid):
            if name.startswith(".tmp."):
                info = _inspect(fs.lstat, directory / name, directory.parent, euid)
                if info is not None and stat.S_ISLNK(info.st_mode):
                    links.append(str(directory / name))
                elif info is not None and stat.S_ISREG(info.st_mode):
                    leftovers.append(f"secrets/{name}")
    return tuple(leftovers), tuple(links)


def _secret_status(
    fs: Filesystem, path: Path, data: Path, secret: init.Secret, euid: int
) -> init.Status:
    if _inspect(fs.lstat, path, data, euid) is None:
        return "missing"
    try:
        value = fs.read_text(path).rstrip("\n")
    except PermissionError:
        if euid == 0:
            raise
        return "unreadable"
    return "ok" if init.valid(value, secret.charset, secret.length) else "invalid"


def gather(fs: Filesystem, data: Path, euid: int) -> init.Facts:
    """Read only statuses; never retain secret text in facts."""
    data = data.absolute()
    links = _symlinks(fs, data, euid)
    if links:
        return init.Facts(
            (), (), webhook_empty=False, smtp_exists=False, leftovers=(), symlinks=links
        )
    statuses: list[init.Status] = []
    guards = []
    for secret in init.SECRETS:
        path = data / "secrets" / secret.name
        statuses.append(_secret_status(fs, path, data, secret, euid))
        guards.append(
            secret.guard is not None
            and _inspect(fs.lstat, data / secret.guard, data, euid) is not None
        )
    webhook = _inspect(fs.lstat, data / "secrets" / init.WEBHOOK, data, euid)
    empty = webhook is None or webhook.st_size == 0
    leftovers, links = _leftovers(fs, data / "secrets", euid)
    return init.Facts(
        tuple(statuses),
        tuple(guards),
        empty,
        _inspect(fs.lstat, data / "secrets" / init.SMTP, data, euid) is not None,
        tuple(leftovers),
        tuple(links),
    )


def _s3_text(fs: Filesystem, data: Path) -> str:
    values = []
    for spec in init.SECRETS[:2]:
        value = fs.read_text(data / "secrets" / spec.name).rstrip("\n")
        if not init.valid(value, spec.charset, spec.length):
            message = "canonical key changed during init"
            raise CliError(message)
        values.append(value)
    access, secret = values
    return f"AWS_ACCESS_KEY_ID={access}\nAWS_SECRET_ACCESS_KEY={secret}\n"


def generate(
    step: init.GenerateSecret,
    fs: Filesystem,
    data: Path,
    choice: Callable[[str], str],
    out: TextIO,
) -> None:
    secret = step.secret
    for _ in range(10):
        value = "".join(choice(secret.charset) for _ in range(secret.length))
        if init.valid(value, secret.charset, secret.length):
            fs.write_atomic(data / "secrets" / secret.name, value + "\n", 0o600)
            notice = (
                f"regenerated {secret.name} (invalid)"
                if step.invalid
                else f"generated {secret.name}"
            )
            out.write(f"init: {notice}\n")
            return
    msg = "could not generate a valid secret"
    raise CliError(msg)


def execute(
    step: init.Step,
    fs: Filesystem,
    data: Path,
    choice: Callable[[str], str],
    out: TextIO,
) -> None:
    """Execute a single planned operation without printing its values."""
    match step:
        case init.EnsureDir(name, uid, gid, mode):
            path = data / name
            fs.mkdir(path)
            fs.chown(path, uid, gid)
            fs.chmod(path, mode)
        case init.RemoveLeftover(name):
            fs.unlink(data / name)
        case init.GenerateSecret():
            generate(step, fs, data, choice, out)
        case init.WritePlaceholder(name):
            text = init.PLACEHOLDER if name == init.WEBHOOK else ""
            fs.write_atomic(data / "secrets" / name, text, 0o600)
            description = "a placeholder" if name == init.WEBHOOK else "an empty"
            out.write(f"init: wrote {description} {name}\n")
        case init.WriteS3Env(uid, gid):
            path = data / "secrets" / "s3.env"
            fs.write_atomic(path, _s3_text(fs, data), 0o600)
            fs.chown(path, uid, gid)
        case init.SetOwnerMode(name, uid, gid, mode):
            fs.chown(data / name, uid, gid)
            fs.chmod(data / name, mode)
        case _:  # pragma: no mutate  # exhaustive Step
            assert_never(step)  # pragma: no mutate  # unreachable Step


def _step_path(step: init.Step) -> str:
    match step:
        case init.GenerateSecret(secret):
            return f"secrets/{secret.name}"
        case init.WritePlaceholder(name):
            return f"secrets/{name}"
        case init.RemoveLeftover(name):
            return f"{name!r}"
        case init.WriteS3Env():
            return "secrets/s3.env"
        case _:
            return step.name


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    """Gather, refuse or execute, reporting only safe error categories."""
    if not args.dry_run and system.euid != 0:
        msg = "init requires root"
        raise CliError(msg)
    context = "Gather ."
    try:
        info = _inspect(
            system.filesystem.lstat, args.data_dir, args.data_dir, system.euid
        )
        if info is None or not stat.S_ISDIR(info.st_mode):
            msg = "init: data dir must already exist and be a directory"
            raise CliError(msg)
        result = init.plan(
            gather(system.filesystem, args.data_dir, system.euid), args.secrets_owner
        )
        if isinstance(result, init.Incomplete):
            out.write(f"{result.message}\n")
            return 3
        if isinstance(result, init.Refusal):
            raise CliError(result.message)
        for step in result.steps:
            if args.dry_run:
                out.write(f"{step}\n")
            else:
                context = f"{type(step).__name__} {_step_path(step)}"
                execute(step, system.filesystem, args.data_dir, system.choice, out)
        if not args.dry_run:
            out.write("init: ok\n")
    except PlanIncompleteError as exc:
        out.write(f"{exc}\n")
        return 3
    except (OSError, ValueError, OverflowError) as exc:
        msg = f"init: {context} failed: {type(exc).__name__}"
        raise CliError(msg) from None
    return 0
