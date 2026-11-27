# cspell:ignore rustfsadmin
"""The init command against a filesystem fake and local atomic writes."""

import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING, override

import pytest
import tbhbops_cli.o11y.init as init_command
import yaml
from tbhbops.o11y import init
from tbhbops.o11y import render as model
from tbhbops.o11y import validate as model_validate
from tbhbops_cli import cli, o11y
from tbhbops_cli.common import CliError
from tbhbops_cli.o11y import filesystem, rustfs_probe
from tbhbops_cli.o11y import render as command
from tbhbops_cli.o11y import validate as validation
from tbhbops_cli.o11y.runner import ProcessHandle, Result, SubprocessRunner

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DATA = Path("/data")


@dataclass
class Fake:
    files: dict[Path, str] = field(default_factory=dict)
    modes: dict[Path, int] = field(default_factory=lambda: {DATA: stat.S_IFDIR | 0o755})
    owners: dict[Path, tuple[int, int]] = field(default_factory=dict)
    denied: set[Path] = field(default_factory=set)
    denied_calls: set[tuple[str, Path]] = field(default_factory=set)
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def temporary_directory(self) -> Path:
        self.mkdir(Path("/temporary"))
        return Path("/temporary")

    def check(self, operation: str, path: Path) -> None:
        if (operation, path) in self.denied_calls or (
            "open",
            path,
        ) in self.denied_calls:
            msg = "sensitive exception text"
            raise PermissionError(msg)

    def prepare_output(self, path: Path) -> Path:
        for directory in (*reversed(path.parents), path):
            self.mkdir(directory)
        return path

    def lstat(self, path: Path) -> os.stat_result | None:
        self.check("lstat", path)
        if path in self.modes:
            return os.stat_result(
                (
                    self.modes[path],
                    0,
                    0,
                    1,
                    0,
                    0,
                    len(self.files.get(path, "")),
                    0,
                    0,
                    0,
                )
            )
        return None

    def socket_lstat(self, path: Path) -> os.stat_result | None:
        return self.lstat(path)

    def read_text(self, path: Path) -> str:
        self.check("read_text", path)
        if path in self.denied:
            msg = "sensitive exception text"
            raise PermissionError(msg)
        return self.files[path]

    def read_bytes(self, path: Path) -> bytes:
        return self.read_text(path).encode("latin1")

    def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None:
        self.calls.append(("write", path, mode))
        self.files[path] = text.decode("latin1") if isinstance(text, bytes) else text
        self.modes[path] = stat.S_IFREG | mode

    def mkdir(self, path: Path) -> None:
        self.calls.append(("mkdir", path))
        self.modes[path] = stat.S_IFDIR | 0o700

    def chown(self, path: Path, uid: int, gid: int) -> None:
        self.calls.append(("chown", path, uid, gid))
        self.owners[path] = uid, gid

    def chmod(self, path: Path, mode: int) -> None:
        self.calls.append(("chmod", path, mode))
        self.modes[path] = stat.S_IFMT(self.modes[path]) | mode

    def list_dir(self, path: Path) -> tuple[str, ...]:
        self.check("list_dir", path)
        return tuple(sorted(p.name for p in self.modes if p.parent == path))

    def unlink(self, path: Path) -> None:
        self.calls.append(("unlink", path))
        del self.files[path]
        del self.modes[path]

    def rmdir(self, path: Path) -> None:
        self.calls.append(("rmdir", path))
        del self.modes[path]

    def rename(self, source: Path, target: Path) -> None:
        self.calls.append(("rename", source, target))
        for path in list(self.modes):
            if path == source or source in path.parents:
                new = target / path.relative_to(source)
                self.modes[new] = self.modes.pop(path)
                if path in self.files:
                    self.files[new] = self.files.pop(path)


def world(fs: Fake, uid: int = 0) -> cli.System:
    return replace(
        cli.real_system(), filesystem=fs, euid=uid, choice=lambda charset: charset[0]
    )


def run(fs: Fake, *args: str, uid: int = 0) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(
        ["o11y", "init", "--data-dir", str(DATA), *args], world(fs, uid), out, err
    )
    return code, out.getvalue(), err.getvalue()


def test_fresh_and_rerun() -> None:
    fs = Fake()
    code, out, err = run(fs, "--secrets-owner", "123:456")
    assert (code, err) == (0, "")
    assert out == "".join(f"init: generated {s.name}\n" for s in init.SECRETS) + (
        "init: wrote a placeholder alert_discord_webhook_url\n"
        "init: wrote an empty alert_smtp_password\ninit: ok\n"
    )
    for s in init.SECRETS:
        p = DATA / "secrets" / s.name
        assert fs.files[p] == s.charset[0] * s.length + "\n"
        assert fs.owners[p] == (s.uid, s.gid)
        assert fs.modes[p] == stat.S_IFREG | 0o400
        assert fs.files[p].strip() not in out + err
    assert (
        fs.files[DATA / "secrets/s3.env"]
        == "AWS_ACCESS_KEY_ID="
        + "A" * 20
        + "\nAWS_SECRET_ACCESS_KEY="
        + "a" * 40
        + "\n"
    )
    assert fs.owners[DATA / "secrets/s3.env"] == (123, 456)
    assert fs.modes[DATA / "secrets/s3.env"] == stat.S_IFREG | 0o600
    assert fs.files[DATA / "secrets" / init.WEBHOOK] == init.PLACEHOLDER
    assert fs.files[DATA / "secrets" / init.SMTP] == ""
    for d in init.DIRECTORIES:
        assert fs.modes[DATA / d.name] == stat.S_IFDIR | d.mode
        assert fs.owners[DATA / d.name] == (d.uid, d.gid)
    before = fs.files.copy()
    assert run(fs) == (0, "init: ok\n", "")
    assert fs.files == before
    facts = o11y.gather(fs, DATA, 1)
    for s in init.SECRETS:
        assert fs.files[DATA / "secrets" / s.name].strip() not in repr(facts)


def test_root_and_dry_run() -> None:
    fs = Fake()
    assert run(fs, uid=1) == (1, "", "tbhbops o11y init: init requires root\n")
    assert fs.calls == []
    code, out, err = run(fs, "--dry-run", uid=1)
    assert (code, err) == (0, "")
    expected = init.plan(o11y.gather(fs, DATA, 1), (0, 0))
    assert isinstance(expected, init.Plan)
    assert out == "".join(f"{s}\n" for s in expected.steps)
    assert fs.calls == []


@pytest.mark.parametrize("status", ["missing", "invalid", "unreadable"])
def test_refusal_changes_nothing(status: str) -> None:
    name = "grafana_admin_password"
    fs = Fake()
    run(fs)
    p = DATA / "secrets" / name
    if status == "missing":
        fs.unlink(p)
    elif status == "invalid":
        fs.files[p] = "broken"
    else:
        fs.denied.add(p)
    fs.modes[DATA / "grafana/grafana.db"] = stat.S_IFREG
    fs.unlink(DATA / "secrets/rustfs.access_key")
    before = fs.files.copy(), fs.modes.copy(), fs.calls.copy()
    code, out, err = run(
        fs,
        "--dry-run" if status == "unreadable" else "--secrets-owner",
        *([] if status == "unreadable" else ["0:0"]),
        uid=1 if status == "unreadable" else 0,
    )
    assert code == (3 if status == "unreadable" else 1)
    assert name in out + err
    assert (fs.files, fs.modes, fs.calls) == before
    assert "sensitive exception text" not in err


@pytest.mark.parametrize("status", ["missing", "invalid", "ok"])
def test_upgrade_from_grafana_owner_release(status: str) -> None:
    fs = Fake()
    run(fs)
    password = DATA / "secrets/grafana_agents_password"
    if status == "missing":
        fs.unlink(password)
    elif status == "invalid":
        fs.files[password] = "broken"
    fs.modes[DATA / "grafana/grafana.db"] = stat.S_IFREG
    before = fs.files.copy()
    facts = o11y.gather(fs, DATA, 0)
    assert facts.statuses == ("ok", "ok", "ok", status)
    assert facts.guards == (False, False, True, False)
    code, out, err = run(fs)
    assert (code, err) == (0, "")
    notice = {
        "missing": "init: generated grafana_agents_password\n",
        "invalid": "init: regenerated grafana_agents_password (invalid)\n",
        "ok": "",
    }[status]
    assert out == notice + "init: ok\n"
    assert fs.files[password] == "a" * 32 + "\n"
    assert fs.owners[password] == (472, 472)
    assert fs.modes[password] == stat.S_IFREG | 0o400
    assert {p: value for p, value in fs.files.items() if p != password} == {
        p: value for p, value in before.items() if p != password
    }


SYMLINKS = [
    DATA,
    Path("/"),
    *(DATA / d.name for d in init.DIRECTORIES),
    *(DATA / s.guard for s in init.SECRETS if s.guard is not None),
    *(
        DATA / "secrets" / n
        for n in [
            *(s.name for s in init.SECRETS),
            init.WEBHOOK,
            init.SMTP,
            "s3.env",
            ".tmp.s3.env",
            ".tmp.unknown",
            ".tmp.unsafe\nname",
        ]
    ),
]


@pytest.mark.parametrize("path", SYMLINKS)
def test_symlinks_are_refused(path: Path) -> None:
    fs = Fake()
    run(fs)
    fs.modes[path] = stat.S_IFLNK
    before = fs.calls.copy()
    assert run(fs) == (
        1,
        "",
        (
            "tbhbops o11y init: init: data dir must already exist and be a directory\n"
            if path == DATA
            else (
                f"tbhbops o11y init: refusing symlink {str(path)!r}; "
                "nothing was changed\n"
            )
        ),
    )
    assert fs.calls == before


def test_only_regular_leftovers_are_removed() -> None:
    fs = Fake()
    run(fs)
    fs.write_atomic(DATA / "secrets/.tmp.old", "interrupted", 0o600)
    fs.modes[DATA / "secrets/.tmp.directory"] = stat.S_IFDIR
    fs.write_atomic(DATA / "secrets/keep", "keep", 0o600)
    assert run(fs)[0] == 0
    assert DATA / "secrets/.tmp.old" not in fs.files
    assert DATA / "secrets/.tmp.directory" in fs.modes
    assert fs.files[DATA / "secrets/keep"] == "keep"


def test_empty_webhook_and_nonempty_smtp() -> None:
    fs = Fake()
    run(fs)
    fs.files[DATA / "secrets" / init.WEBHOOK] = ""
    fs.files[DATA / "secrets" / init.SMTP] = "smtp-test-value"
    assert run(fs) == (
        0,
        "init: wrote a placeholder alert_discord_webhook_url\ninit: ok\n",
        "",
    )
    assert fs.files[DATA / "secrets" / init.SMTP] == "smtp-test-value"


def test_generated_values_are_revalidated_and_bounded() -> None:
    fs = Fake()
    out = io.StringIO()
    step = init.GenerateSecret(init.Secret("test", "rustfsadminX", 11, "guard", 0, 0))
    values = iter("rustfsadmin" + "X" * 11)
    o11y.generate(step, fs, DATA, lambda _: next(values), out)
    assert fs.files[DATA / "secrets/test"] == "X" * 11 + "\n"
    assert fs.calls == [("write", DATA / "secrets/test", 0o600)]
    fs.calls.clear()
    draws = []

    def invalid_choice(charset: str) -> str:
        draws.append(charset)
        return "!"

    with pytest.raises(CliError, match=r"^could not generate a valid secret$"):
        o11y.generate(step, fs, DATA, invalid_choice, out)
    assert draws == [step.secret.charset] * (10 * step.secret.length)
    assert fs.calls == []


@pytest.mark.parametrize("error", [OSError, ValueError, OverflowError, UnicodeError])
def test_io_errors_do_not_quote_exception_text(error: type[Exception]) -> None:
    class Broken(Fake):
        @override
        def lstat(self, path: Path) -> os.stat_result | None:
            msg = f"sensitive exception text {path}"
            raise error(msg)

    assert run(Broken()) == (
        1,
        "",
        f"tbhbops o11y init: init: Gather . failed: {error.__name__}\n",
    )


@pytest.mark.parametrize("value", ["", "1", "1:2:3", "a:0", "0:-1", "\uff10:0", "0:"])
def test_invalid_owner_is_usage(value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(
            ["o11y", "init", "--data-dir", "/data", "--secrets-owner", value]
        )
    assert exc.value.code == 2


def test_owner_diagnostic() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=r"^expected UID:GID$"):
        o11y.owner("bad")


@pytest.mark.parametrize("argv", [["o11y"], ["o11y", "init"]])
def test_required_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], "o11y prepare the observability stack"),
        (["o11y"], "init prepare host data directories and secrets"),
        (["o11y"], "render build a content-addressed release"),
    ],
)
def test_help(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args([*argv, "--help"])
    assert exc.value.code == 0
    assert expected in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize(
    ("value", "status"),
    [
        (None, "missing"),
        ("broken", "invalid"),
        ("A" * 19 + "X\n", "ok"),
        ("A" * 20 + " \n", "invalid"),
    ],
)
def test_gather_statuses(value: str | None, status: init.Status) -> None:
    fs = Fake()
    if value is not None:
        fs.write_atomic(DATA / "secrets/rustfs.access_key", value, 0o400)
    facts = o11y.gather(fs, DATA, 1)
    assert facts.statuses == (status, "missing", "missing", "missing")
    assert facts.guards == (False, False, False, False)


def test_unreadable_status_and_plan_incomplete() -> None:
    fs = Fake()
    run(fs)
    fs.denied.add(DATA / "secrets/rustfs.secret_key")
    assert o11y.gather(fs, DATA, 1).statuses == ("ok", "unreadable", "ok", "ok")
    assert run(fs, "--dry-run", uid=1) == (
        3,
        ("can't read rustfs.secret_key without root; plan incomplete\n"),
        "",
    )


def test_placeholder_writes_are_private() -> None:
    fs = Fake()
    for name in (init.WEBHOOK, init.SMTP):
        o11y.execute(
            init.WritePlaceholder(name), fs, DATA, lambda _: "!", io.StringIO()
        )
        assert fs.calls[-1] == ("write", DATA / "secrets" / name, 0o600)


def test_s3_env_preserves_keys_ending_in_x() -> None:
    fs = Fake()
    for s in init.SECRETS:
        fs.write_atomic(DATA / "secrets" / s.name, "X" * s.length + "\n", 0o400)
    o11y.execute(init.WriteS3Env(0, 0), fs, DATA, lambda _: "!", io.StringIO())
    assert (
        fs.files[DATA / "secrets/s3.env"]
        == "AWS_ACCESS_KEY_ID="
        + "X" * 20
        + "\nAWS_SECRET_ACCESS_KEY="
        + "X" * 40
        + "\n"
    )


def test_symlink_facts_have_only_the_unsafe_path() -> None:
    fs = Fake(modes={DATA: stat.S_IFLNK})
    assert o11y.gather(fs, DATA, 1) == init.Facts(
        (),
        (),
        webhook_empty=False,
        smtp_exists=False,
        leftovers=(),
        symlinks=(str(DATA),),
    )


def test_real_filesystem(tmp_path: Path) -> None:
    fs = o11y.LocalFilesystem()
    assert fs.root is None
    data = tmp_path.resolve() / "data"
    mask = os.umask(0)
    try:
        fs.mkdir(data)
    finally:
        os.umask(mask)
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    fs.mkdir(data)
    path = data / "secret"
    fs.write_atomic(path, "test-value\n", 0o400)
    assert fs.read_text(path) == "test-value\n"
    info = fs.lstat(path)
    assert info is not None
    assert stat.S_IMODE(info.st_mode) == 0o400
    fs.chown(path, path.stat().st_uid, path.stat().st_gid)
    fs.chmod(path, 0o600)
    info = fs.lstat(path)
    assert info is not None
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert fs.list_dir(data) == ("secret",)
    fs.write_atomic(path, "replacement", 0o600)
    assert fs.read_text(path) == "replacement"
    fs.write_atomic(path, "raw\r\nnewlines\r", 0o600)
    assert fs.read_text(path) == "raw\r\nnewlines\r"
    assert fs.list_dir(data) == ("secret",)
    fs.unlink(path)
    assert fs.lstat(path) is None


@pytest.mark.parametrize("use_path_flag", [False, True])
@pytest.mark.parametrize("read_directory", [False, True])
def test_parent_open_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_path_flag: bool,
    read_directory: bool,
) -> None:
    # A synthetic bit exercises both platform branches even on macOS.
    path_flag = 1 << 29
    if use_path_flag:
        monkeypatch.setattr(os, "O_PATH", path_flag, raising=False)
    else:
        monkeypatch.delattr(os, "O_PATH", raising=False)
    original = os.open
    calls: list[str] = []
    expected = path_flag if use_path_flag and not read_directory else os.O_RDONLY

    def directory_open(name: str, flags: int, mode: int, *, dir_fd: int | None) -> int:
        if sys.platform != "win32":
            if name == ".tmp.child":
                assert flags == os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            else:
                assert flags == expected | os.O_DIRECTORY | os.O_NOFOLLOW
                calls.append(name)
        return original(name, flags & ~path_flag, mode, dir_fd=dir_fd)

    root = tmp_path.resolve()
    (root / "nested").mkdir()
    child = root / "nested/child"
    child.touch()
    fs = o11y.LocalFilesystem()
    fs.root = root
    monkeypatch.setattr(os, "open", directory_open)
    if read_directory:
        fs.write_atomic(child, "fixture", 0o600)
    else:
        assert fs.lstat(child) is not None
    assert calls == [str(root), "nested"]


@pytest.mark.skipif(
    sys.platform != "linux" or o11y.effective_uid() == 0,
    reason="Linux O_PATH permission checks require a non-root test user",
)
@pytest.mark.parametrize("mode", [0o311, 0o111])
@pytest.mark.parametrize("trusted_root", [False, True])
def test_lstat_through_search_only_directory(
    tmp_path: Path, mode: int, trusted_root: bool
) -> None:
    fs = o11y.LocalFilesystem()
    parent = tmp_path.resolve() / "grafana"
    parent.mkdir()
    child = parent / "grafana.db"
    child.write_text("fixture", encoding="utf-8")
    expected = child.stat()
    if trusted_root:
        fs.root = parent
    parent.chmod(mode)
    try:
        assert fs.lstat(child) == expected
        assert fs.lstat(parent / "missing") is None
        assert fs.read_text(child) == "fixture"
        assert fs.read_bytes(child) == b"fixture"
        with pytest.raises(PermissionError):
            fs.list_dir(parent)
        assert stat.S_IMODE(parent.stat().st_mode) == mode
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("use_path_flag", [False, True])
def test_lstat_refuses_symlinked_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_path_flag: bool
) -> None:
    if not use_path_flag:
        monkeypatch.delattr(os, "O_PATH", raising=False)
    fs = o11y.LocalFilesystem()
    root = tmp_path.resolve()
    target = root / "target"
    target.mkdir()
    (target / "child").touch()
    link = root / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        fs.lstat(link / "child")


def test_real_filesystem_refuses_symlinks_and_files_in_place_of_directories(
    tmp_path: Path,
) -> None:
    fs = o11y.LocalFilesystem()
    data = tmp_path.resolve() / "links"
    fs.mkdir(data)
    path = data / "secret"
    target = data / "target"
    target.write_text("untouched", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        fs.mkdir(target)
    (data / ".tmp.secret").symlink_to(target)
    with pytest.raises(FileExistsError):
        fs.write_atomic(path, "unsafe", 0o600)
    assert target.read_text(encoding="utf-8") == "untouched"
    (data / "link").symlink_to(data, target_is_directory=True)
    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        fs.read_text(data / "link/target")
    info = fs.lstat(data / "link")
    assert info is not None
    assert stat.S_ISLNK(info.st_mode)
    dangling = data / "dangling"
    dangling.symlink_to(data / "missing")
    with pytest.raises(OSError, match="Too many levels"):
        fs.chown(dangling, path.parent.stat().st_uid, path.parent.stat().st_gid)
    link = data / "file-link"
    link.symlink_to(target)
    fs.chmod(target, 0o600)
    with pytest.raises(OSError, match="Too many levels"):
        fs.chmod(link, 0o400)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_effective_uid_and_unix_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        assert o11y.effective_uid() == os.geteuid()
    monkeypatch.setattr(sys, "platform", "win32")
    assert o11y.effective_uid() == -1
    with pytest.raises(CliError, match=r"^init filesystem operations require Unix$"):
        o11y.LocalFilesystem().lstat(DATA)
    with pytest.raises(CliError, match=r"^init filesystem operations require Unix$"):
        o11y.LocalFilesystem().chown(DATA, 0, 0)


@pytest.mark.parametrize("value", ["broken", "X" * 20 + " \n"])
def test_keys_that_change_after_gathering_cannot_enter_s3_env(value: str) -> None:
    fs = Fake()
    run(fs)
    fs.files[DATA / "secrets/rustfs.access_key"] = value
    fs.calls.clear()
    with pytest.raises(CliError, match=r"^canonical key changed during init$"):
        o11y.execute(init.WriteS3Env(0, 0), fs, DATA, lambda _: "!", io.StringIO())
    assert fs.calls == []


def test_dry_run_never_prints_existing_values() -> None:
    fs = Fake()
    run(fs)
    values = ["X" * s.length for s in init.SECRETS]
    for spec, value in zip(init.SECRETS, values, strict=True):
        fs.files[DATA / "secrets" / spec.name] = value
    for args in [("--dry-run",), ()]:
        code, out, err = run(fs, *args)
        assert (code, err) == (0, "")
        for value in values:
            assert value not in out + err


@pytest.mark.parametrize(
    ("operation", "relative"),
    [
        ("lstat", "rustfs/.rustfs.sys"),
        ("lstat", "secrets/rustfs.access_key"),
        ("list_dir", "secrets"),
        ("open", "grafana/grafana.db"),
        ("open", "secrets"),
        ("read_text", "secrets/rustfs.access_key"),
    ],
)
@pytest.mark.parametrize("uid", [0, 1])
def test_inaccessible_facts(operation: str, relative: str, uid: int) -> None:
    fs = Fake()
    assert run(fs)[0] == 0
    fs.denied_calls.add((operation, DATA / relative))
    before = fs.calls.copy()
    code, out, err = run(fs, "--dry-run", uid=uid)
    if uid == 0:
        assert (code, out, err) == (
            1,
            "",
            "tbhbops o11y init: init: Gather . failed: PermissionError\n",
        )
    else:
        assert (code, err) == (3, "")
        assert "plan incomplete" in out
        assert relative in out or Path(relative).name in out
    assert "sensitive exception text" not in out + err
    assert fs.calls == before


@pytest.mark.skipif(o11y.effective_uid() == 0, reason="requires a non-root process")
def test_non_root_inaccessible_directory(tmp_path: Path) -> None:
    data = tmp_path.resolve()
    private = data / "grafana"
    private.mkdir(mode=0o700)
    private.chmod(0)
    try:
        out, err = io.StringIO(), io.StringIO()
        code = cli.run(
            ["o11y", "init", "--data-dir", str(data), "--dry-run"],
            cli.real_system(),
            out,
            err,
        )
        assert code == 3
        assert err.getvalue() == ""
        assert (
            out.getvalue()
            == "init: can't inspect 'grafana/grafana.db'; plan incomplete\n"
        )
    finally:
        private.chmod(0o700)


@pytest.mark.parametrize("mode", [None, stat.S_IFREG, stat.S_IFLNK])
def test_data_directory_must_exist(mode: int | None) -> None:
    fs = Fake()
    if mode is None:
        del fs.modes[DATA]
    else:
        fs.modes[DATA] = mode
    for args in [(), ("--dry-run",)]:
        assert run(fs, *args) == (
            1,
            "",
            "tbhbops o11y init: init: data dir must already exist and be a directory\n",
        )
    assert fs.calls == []


@pytest.mark.parametrize("invalid", [False, True])
def test_generation_notice(invalid: bool) -> None:
    fs = Fake()
    assert run(fs)[0] == 0
    path = DATA / "secrets/grafana_admin_password"
    if invalid:
        fs.files[path] = "broken"
    else:
        fs.unlink(path)
    expected = (
        "regenerated grafana_admin_password (invalid)"
        if invalid
        else "generated grafana_admin_password"
    )
    assert run(fs) == (0, f"init: {expected}\ninit: ok\n", "")


@pytest.mark.parametrize("operation", ["write", "fchmod", "fsync", "rename"])
def test_atomic_failure_removes_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    fs = o11y.LocalFilesystem()
    path = tmp_path.resolve() / "secret"

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "injected failure"
        raise OSError(msg)

    if operation == "write":
        original = os.fdopen

        def broken_stream(fd: int, mode: str) -> IO[str]:
            stream = original(fd, mode)
            monkeypatch.setattr(stream, "write", fail)
            return stream

        monkeypatch.setattr(os, "fdopen", broken_stream)
    else:
        monkeypatch.setattr(os, operation, fail)
    with pytest.raises(OSError, match="injected failure"):
        fs.write_atomic(path, "test-value", 0o600)
    assert list(tmp_path.iterdir()) == []


def test_mkdir_does_not_create_ancestors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        o11y.LocalFilesystem().mkdir(tmp_path.resolve() / "missing/child")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["file", "fifo"])
def test_ancestor_must_be_directory(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path.resolve() / "ancestor"
    if kind == "fifo":
        if sys.platform != "win32":
            os.mkfifo(ancestor)
    else:
        ancestor.touch()
    original = os.open

    def directory_open(name: str, flags: int, mode: int, *, dir_fd: int | None) -> int:
        if sys.platform != "win32":
            assert flags & os.O_DIRECTORY
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", directory_open)
    with pytest.raises(NotADirectoryError):
        o11y.LocalFilesystem().lstat(ancestor / "child")


def test_error_names_execution_step() -> None:
    class Broken(Fake):
        @override
        def chown(self, path: Path, uid: int, gid: int) -> None:
            if path == DATA / "secrets/grafana_admin_password":
                msg = "sensitive exception text"
                raise PermissionError(msg)
            super().chown(path, uid, gid)

    code, out, err = run(Broken())
    assert code == 1
    assert (
        err == "tbhbops o11y init: init: SetOwnerMode secrets/grafana_admin_password "
        "failed: PermissionError\n"
    )
    assert "sensitive exception text" not in out + err


@pytest.mark.parametrize(
    ("relative", "occurrence"),
    [
        (".", 1),
        ("..", 1),
        ("secrets", 2),
        ("secrets/.tmp.old", 1),
        ("secrets/rustfs.access_key", 2),
        ("rustfs/.rustfs.sys", 2),
        ("secrets/alert_discord_webhook_url", 2),
        ("secrets/alert_smtp_password", 2),
    ],
)
@pytest.mark.parametrize("uid", [0, 1])
def test_each_fact_inspection_names_relative_path(
    relative: str, occurrence: int, uid: int
) -> None:
    class Denied(Fake):
        seen: int = 0

        @override
        def lstat(self, path: Path) -> os.stat_result | None:
            if path == (DATA / relative).resolve():
                self.seen += 1
                if self.seen == occurrence:
                    raise PermissionError
            return super().lstat(path)

    fs = Denied()
    fs.modes[DATA / "secrets"] = stat.S_IFDIR
    fs.modes[DATA / "secrets/.tmp.old"] = stat.S_IFREG
    expected = (
        (1, "", "tbhbops o11y init: init: Gather . failed: PermissionError\n")
        if uid == 0
        else (3, f"init: can't inspect {relative!r}; plan incomplete\n", "")
    )
    assert run(fs, "--dry-run", uid=uid) == expected
    assert fs.calls == []


@pytest.mark.parametrize(
    ("step", "path"),
    [
        (init.EnsureDir("loki", 10001, 10001), "loki"),
        (init.RemoveLeftover("secrets/.tmp.old"), "'secrets/.tmp.old'"),
        (init.GenerateSecret(init.SECRETS[0]), "secrets/rustfs.access_key"),
        (init.WritePlaceholder(init.WEBHOOK), "secrets/alert_discord_webhook_url"),
        (init.WriteS3Env(0, 0), "secrets/s3.env"),
        (init.SetOwnerMode("secrets/s3.env", 0, 0, 0o600), "secrets/s3.env"),
    ],
)
def test_io_error_context_for_each_step(
    step: init.Step, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def planned(_facts: init.Facts, _owner: tuple[int, int]) -> init.Plan:
        return init.Plan((step,))

    monkeypatch.setattr(init, "plan", planned)

    def fail(*_args: object) -> None:
        raise PermissionError

    monkeypatch.setattr(init_command, "execute", fail)
    assert run(Fake()) == (
        1,
        "",
        (
            f"tbhbops o11y init: init: {type(step).__name__} {path} "
            "failed: PermissionError\n"
        ),
    )


def test_init_help_reports_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["o11y", "init", "--help"])
    assert exc.value.code == 0
    assert (
        "Prepare an existing data directory. Exit 3 means the plan is incomplete; "
        "refusal or I/O errors exit 1, usage errors exit 2."
        in " ".join(capsys.readouterr().out.split())
    )


def test_leftover_error_escapes_entry_name() -> None:
    class Broken(Fake):
        @override
        def unlink(self, path: Path) -> None:
            raise PermissionError

    fs = Broken()
    assert run(fs)[0] == 0
    name = "secrets/.tmp.unsafe\nname\x1b"
    fs.write_atomic(DATA / name, "interrupted", 0o600)
    code, _out, err = run(fs)
    assert code == 1
    assert err == (
        f"tbhbops o11y init: init: RemoveLeftover {name!r} failed: PermissionError\n"
    )
    assert name not in err
    assert err.count("\n") == 1


def test_atomic_cleanup_failure_preserves_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fdopen
    failure = ValueError("original write failure")
    unlinks = []

    def fail_write(_text: str) -> int:
        raise failure

    def broken_stream(fd: int, mode: str) -> IO[str]:
        stream = original(fd, mode)
        monkeypatch.setattr(stream, "write", fail_write)
        return stream

    def fail_unlink(name: str, *, dir_fd: int) -> None:
        unlinks.append((name, dir_fd))
        message = "cleanup failure"
        raise OSError(message)

    monkeypatch.setattr(os, "fdopen", broken_stream)
    monkeypatch.setattr(os, "unlink", fail_unlink)
    with pytest.raises(ValueError, match="original write failure") as exc:
        o11y.LocalFilesystem().write_atomic(
            tmp_path.resolve() / "secret", "test-value", 0o600
        )
    assert exc.value is failure
    assert len(unlinks) == 1
    assert unlinks[0][0] == ".tmp.secret"
    assert unlinks[0][1] >= 0


# Render fixture: the email blocks represent provisioning and its deletion.
SOURCE = Path("/stack")
OUTPUT = Path("/output")
RENDER_ENV = {
    "O11Y_DATA_DIR": "/srv/data",
    "O11Y_GRAFANA_ROOT_URL": "https://grafana.example.invalid/",
    "O11Y_GRAFANA_OWNER_LOGIN": "owner@example.invalid",
}
RENDER_MAIL = {
    "O11Y_ALERT_EMAIL_TO": "recipient@example.invalid",
    "O11Y_ALERT_SMTP_HOST": "relay.example.invalid",
    "O11Y_ALERT_SMTP_PORT": "587",
    "O11Y_ALERT_SMTP_USER": "mailer@example.invalid",
    "O11Y_ALERT_SMTP_FROM": "sender@example.invalid",
}
ALERTING = (
    "uid: discord\n"
    "# @O11Y_ALERT_EMAIL@ begin\n"
    "type: email\n"
    "addresses: $__env{O11Y_ALERT_EMAIL_TO}\n"
    "# @O11Y_ALERT_EMAIL@ end\n"
    "# @O11Y_ALERT_EMAIL@ off begin\n"
    "deleteContactPoints: email\n"
    "# @O11Y_ALERT_EMAIL@ off end\n"
)


def render_fixture(fs: o11y.Filesystem, source: Path = SOURCE) -> None:
    fs.mkdir(source)
    fs.mkdir(source / "config")
    fs.mkdir(source / "config/grafana")
    fs.mkdir(source / "config/grafana/provisioning")
    fs.mkdir(source / "config/loki")
    fs.mkdir(source / "config/loki/empty")
    fs.write_atomic(source / "compose.yaml", b"compose\r\n", 0o644)
    fs.write_atomic(source / "compose.tools.yaml", b"tools\n", 0o644)
    fs.write_atomic(
        source / "config/grafana/provisioning/alerting.yaml", ALERTING, 0o644
    )
    fs.write_atomic(
        source / "config/grafana/rules.yaml",
        "@O11Y_DISK_RESERVE_GB@ @O11Y_DISK_RESERVE_BYTES@\n",
        0o644,
    )
    fs.write_atomic(source / "config/loki/config.yaml", b"loki\x00\xff", 0o600)
    fs.write_atomic(source / "config/loki/executable", b"executable", 0o710)


def render_run(
    fs: o11y.Filesystem,
    env: dict[str, str] | None = None,
    *args: str,
    source: Path = SOURCE,
    output: Path = OUTPUT,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    system = replace(
        cli.real_system(), filesystem=fs, env=RENDER_ENV if env is None else env
    )
    code = cli.run(
        ["o11y", "render", "--source", str(source), "--out", str(output), *args],
        system,
        out,
        err,
    )
    return code, out.getvalue(), err.getvalue()


def targets(fs: Fake) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in fs.files[OUTPUT / "release/.env"].splitlines()
        if line.startswith("O11Y_CONFIG_")
    )


def rendered_fake() -> Fake:
    fs = Fake()
    render_fixture(fs)
    assert render_run(fs) == (0, "", "")
    return fs


def o11y_source(tmp_path: Path) -> Path:
    source = next(
        parent / "o11y"
        for parent in Path(__file__).parents
        if (parent / "o11y/compose.yaml").is_file()
    )
    # Copy Bazel's runfile symlinks as files for the renderer. Supply fixed
    # resolver fixtures so rendering also works without private ops files.
    copy = shutil.copytree(source, tmp_path / "source")
    (copy / "config/ts").mkdir(exist_ok=True)
    (copy / "config/ts/resolv.conf").write_text(
        "nameserver 198.51.100.41\nnameserver 198.51.100.106\n", encoding="utf-8"
    )
    (copy / "config/grafana/resolv.conf").write_text(
        "nameserver 127.0.0.11\n"
        "nameserver 198.51.100.41\n"
        "nameserver 198.51.100.106\n"
        "nameserver 198.51.100.129\n"
        "search tailnet.example.invalid us.example.invalid example.invalid\n"
        "options edns0 trust-ad ndots:0\n",
        encoding="utf-8",
    )
    return copy


def test_render_grafana_resolver_and_exclusive_mount(tmp_path: Path) -> None:
    source = o11y_source(tmp_path)
    output = tmp_path / "rendered"
    assert render_run(o11y.LocalFilesystem(), source=source, output=output) == (
        0,
        "",
        "",
    )
    env = dict(
        line.split("=", 1)
        for line in (output / "release/.env").read_text().splitlines()
    )
    grafana = Path(env["O11Y_CONFIG_GRAFANA"])
    resolver = grafana / "resolv.conf"
    assert resolver.is_file()
    assert resolver.read_bytes() == (source / "config/grafana/resolv.conf").read_bytes()
    directives = [
        line.split()
        for line in resolver.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert directives == [
        ["nameserver", "127.0.0.11"],
        ["nameserver", "198.51.100.41"],
        ["nameserver", "198.51.100.106"],
        ["nameserver", "198.51.100.129"],
        ["search", "tailnet.example.invalid", "us.example.invalid", "example.invalid"],
        ["options", "edns0", "trust-ad", "ndots:0"],
    ]
    digest = hashlib.sha256(resolver.read_bytes()).hexdigest()
    assert f"{digest}  ./resolv.conf\n" in (grafana / "MANIFEST.sha256").read_text()
    services = yaml.safe_load((output / "release/compose.yaml").read_text())["services"]
    mounts = [
        (name, volume)
        for name, service in services.items()
        for volume in service.get("volumes", [])
        if "/etc/resolv.conf" in volume
    ]
    assert mounts == [
        ("grafana", "${O11Y_CONFIG_GRAFANA:?}/resolv.conf:/etc/resolv.conf:ro")
    ]


def test_render_identical_reuses_targets_and_recreates_release() -> None:
    fs = rendered_fake()
    before = fs.files.copy(), fs.modes.copy()
    existing = {
        p
        for p in fs.modes
        if OUTPUT / "config" in p.parents and ".incoming-" not in str(p)
    }
    fs.write_atomic(OUTPUT / "release/stale", "remove", 0o600)
    fs.calls.clear()
    assert render_run(fs) == (0, "", "")
    assert (fs.files, fs.modes) == before
    assert not any(call[1] in existing for call in fs.calls)
    assert ("rmdir", OUTPUT / "release") in fs.calls
    # With an explicit root, different output directories contain identical bytes.
    assert render_run(fs, None, "--config-root", "/remote/config") == (0, "", "")
    a = {
        str(p.relative_to(OUTPUT)): data
        for p, data in fs.files.items()
        if OUTPUT in p.parents
    }
    assert render_run(
        fs, None, "--config-root", "/remote/config", output=Path("/second")
    ) == (0, "", "")
    b = {
        str(p.relative_to("/second")): data
        for p, data in fs.files.items()
        if Path("/second") in p.parents
    }
    assert a == b


@pytest.mark.parametrize(
    ("change", "changed"),
    [
        pytest.param("file", "LOKI", id="bats-changed-file-only-own-directory"),
        pytest.param("reserve", "GRAFANA", id="bats-disk-reserve-only-grafana"),
        pytest.param("email", "GRAFANA", id="bats-email-on-off"),
    ],
)
def test_render_changes_only_affected_directory(change: str, changed: str) -> None:
    fs = rendered_fake()
    before = targets(fs)
    env = RENDER_ENV.copy()
    if change == "file":
        fs.files[SOURCE / "config/loki/config.yaml"] += "#"
    elif change == "reserve":
        env["O11Y_DISK_RESERVE_GB"] = "7"
    else:
        env |= RENDER_MAIL
    assert render_run(fs, env) == (0, "", "")
    after = targets(fs)
    assert {key for key in before if before[key] != after[key]} == {
        f"O11Y_CONFIG_{changed}"
    }
    grafana = Path(after["O11Y_CONFIG_GRAFANA"])
    if change == "reserve":
        assert fs.files[grafana / "rules.yaml"] == "7 7516192768\n"
    if change == "email":
        assert (
            fs.files[grafana / "provisioning/alerting.yaml"]
            == "uid: discord\ntype: email\naddresses: $__env{O11Y_ALERT_EMAIL_TO}\n"
        )
        assert render_run(fs) == (0, "", "")
        assert targets(fs) == before
        grafana = Path(before["O11Y_CONFIG_GRAFANA"])
        assert (
            fs.files[grafana / "provisioning/alerting.yaml"]
            == "uid: discord\ndeleteContactPoints: email\n"
        )
        assert "O11Y_ALERT_" not in fs.files[OUTPUT / "release/.env"]
    assert all(
        "@example.invalid" not in data
        for path, data in fs.files.items()
        if OUTPUT / "config" in path.parents
    )


def test_render_release_env_permissions_and_manifests() -> None:
    fs = rendered_fake()
    assert fs.modes[OUTPUT / "config"] == stat.S_IFDIR | 0o700
    assert fs.modes[OUTPUT / "release"] == stat.S_IFDIR | 0o700
    release_files = {p.name for p in fs.files if p.parent == OUTPUT / "release"}
    assert release_files == {
        "compose.yaml",
        "compose.tools.yaml",
        "dummy-s3.env",
        ".env",
        ".env.validate",
        "MANIFEST.sha256",
    }
    assert (
        fs.files[OUTPUT / "release/dummy-s3.env"]
        == "AWS_ACCESS_KEY_ID=dummy-not-a-secret\n"
        "AWS_SECRET_ACCESS_KEY=dummy-not-a-secret\n"
    )
    assert fs.read_bytes(OUTPUT / "release/compose.yaml") == b"compose\r\n"
    check_render_modes(fs)
    env = fs.files[OUTPUT / "release/.env"].splitlines()
    validate = fs.files[OUTPUT / "release/.env.validate"].splitlines()
    assert env[:-1] == validate[:-1]
    assert env[-1] == "O11Y_S3_ENV_FILE=/srv/data/secrets/s3.env"
    assert validate[-1] == "O11Y_S3_ENV_FILE=dummy-s3.env"
    assert env[:4] == [
        *(f"{n}={v}" for n, v in RENDER_ENV.items()),
        "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD=0.90",
    ]
    assert tuple(targets(fs)) == ("O11Y_CONFIG_GRAFANA", "O11Y_CONFIG_LOKI")
    for value in targets(fs).values():
        assert fs.modes[Path(value)] == stat.S_IFDIR | 0o755
        assert Path(value) / "MANIFEST.sha256" in fs.files
    check_render_manifests(fs)
    grafana = Path(targets(fs)["O11Y_CONFIG_GRAFANA"])
    assert fs.files[grafana / "rules.yaml"] == "50 53687091200\n"


def check_render_modes(fs: Fake) -> None:
    for path, mode in fs.modes.items():
        if OUTPUT / "release" in path.parents:
            assert mode == stat.S_IFREG | 0o600
        elif OUTPUT / "config" in path.parents:
            expected = (
                0o755 if stat.S_ISDIR(mode) or path.name == "executable" else 0o644
            )
            assert stat.S_IMODE(mode) == expected


def check_render_manifests(fs: Fake) -> None:
    for path, text in fs.files.items():
        if OUTPUT in path.parents and path.name == "MANIFEST.sha256":
            files = {
                str(p.relative_to(path.parent)): fs.read_bytes(p)
                for p in fs.files
                if path.parent in p.parents and p.name != "MANIFEST.sha256"
            }
            expected = "".join(
                f"{hashlib.sha256(files[n]).hexdigest()}  ./{n}\n"
                for n in sorted(files)
            )
            assert text == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        pytest.param(
            "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD", "0.99", id="bats-wal-override"
        ),
        pytest.param("O11Y_GRAFANA_PORT", "13000", id="bats-port-override"),
        pytest.param("O11Y_SECRETS_OWNER", "1000:1000", id="bats-owner-override"),
        pytest.param("O11Y_CLUSTER", "site-a", id="bats-cluster-only-set"),
        ("O11Y_OTLP_GRPC_PORT", "14317"),
        ("O11Y_OTLP_HTTP_PORT", "14318"),
        ("O11Y_HOST_NAME", "local"),
        ("O11Y_ENVIRONMENT", "test"),
    ],
)
def test_render_optional_env(name: str, value: str) -> None:
    fs = rendered_fake()
    baseline = targets(fs)
    for env_file in (".env", ".env.validate"):
        if name != "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD":
            assert f"{name}=" not in fs.files[OUTPUT / "release" / env_file]
    assert render_run(fs, RENDER_ENV | {name: value}) == (0, "", "")
    assert targets(fs) == baseline
    for env_file in (".env", ".env.validate"):
        assert f"{name}={value}\n" in fs.files[OUTPUT / "release" / env_file]


@pytest.mark.parametrize(
    ("env", "names"),
    [
        pytest.param({}, tuple(RENDER_ENV), id="bats-unset-required"),
        pytest.param(
            RENDER_ENV | {"O11Y_GRAFANA_OWNER_LOGIN": "bad owner"},
            ("O11Y_GRAFANA_OWNER_LOGIN",),
            id="bats-owner-validated-privately",
        ),
        pytest.param(
            RENDER_ENV | {"O11Y_DISK_RESERVE_GB": "08"},
            ("O11Y_DISK_RESERVE_GB",),
            id="bats-bad-reserve",
        ),
        pytest.param(
            RENDER_ENV | {"O11Y_ALERT_EMAIL_TO": "to@example.invalid"},
            tuple(RENDER_MAIL)[1:],
            id="bats-recipient-without-smtp",
        ),
    ],
)
def test_render_refuses_before_writing(
    env: dict[str, str], names: tuple[str, ...]
) -> None:
    fs = Fake()
    code, out, err = render_run(fs, env)
    assert (code, out) == (1, "")
    for name in names:
        assert name in err
    for value in env.values():
        assert value not in err
    assert fs.calls == []


def test_render_unhashable_file_reports_no_value() -> None:
    fs = Fake()
    render_fixture(fs)
    fs.denied.add(SOURCE / "config/loki/config.yaml")
    assert render_run(fs) == (
        1,
        "",
        (
            "tbhbops o11y render: render: RenderConfig config/loki "
            "failed: PermissionError\n"
        ),
    )


def test_render_real_filesystem_and_new_seam_methods(tmp_path: Path) -> None:
    fs = o11y.LocalFilesystem()
    root = tmp_path.resolve()
    source, output = root / "source", root / "nested/output"
    render_fixture(fs, source)
    fs.write_atomic(source / "config/loki/raw-text", "\udc80", 0o644)
    fs.write_atomic(
        source / "config/loki/token-bytes", b"\xff @O11Y_DISK_RESERVE_GB@\n", 0o644
    )
    fs.write_atomic(source / "config/loki/MANIFEST.sha256", b"stale", 0o644)
    assert render_run(fs, source=source, output=output) == (0, "", "")
    files = {
        p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }
    assert render_run(fs, source=source, output=output) == (0, "", "")
    assert {
        p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()
    } == files
    assert stat.S_IMODE((output / "release").stat().st_mode) == 0o700
    assert fs.read_bytes(output / "release/compose.yaml") == b"compose\r\n"
    binary = root / "binary"
    fs.write_atomic(binary, b"\x00\xff\r\n", 0o640)
    assert fs.read_bytes(binary) == b"\x00\xff\r\n"
    fs.rename(binary, root / "renamed")
    assert not binary.exists()
    assert (root / "renamed").read_bytes() == b"\x00\xff\r\n"
    fs.mkdir(root / "empty")
    fs.rmdir(root / "empty")
    assert not (root / "empty").exists()


def test_render_usage_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    cases: list[list[str]] = [
        [],
        ["--source", "/source"],
        ["--out", "/out"],
        ["--source", "/source", "--out", "/out", "--config-root", "relative"],
    ]
    for args in cases:
        with pytest.raises(SystemExit) as exc:
            cli.parser().parse_args(["o11y", "render", *args])
        assert exc.value.code == 2
    with pytest.raises(
        argparse.ArgumentTypeError, match=r"^--config-root must be an absolute path$"
    ):
        command.absolute_path("relative")
    assert command.absolute_path("/absolute") == Path("/absolute")
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["o11y", "render", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--source" in help_text
    assert "--out" in help_text
    assert "--config-root" in help_text


@pytest.mark.parametrize("mode", [stat.S_IFIFO])
def test_render_refuses_special_files(mode: int) -> None:
    fs = Fake()
    render_fixture(fs)
    fs.modes[SOURCE / "config/loki/unsafe"] = mode
    assert render_run(fs) == (
        1,
        "",
        (
            "tbhbops o11y render: render source must contain only "
            "regular files and directories\n"
        ),
    )


def test_render_skips_other_entries_and_cleans_incoming() -> None:
    fs = Fake()
    render_fixture(fs)
    fs.mkdir(OUTPUT / "config/.incoming-loki")
    fs.write_atomic(OUTPUT / "config/.incoming-loki/stale", "stale", 0o600)
    fs.write_atomic(SOURCE / "config/ignore", "not a directory", 0o600)
    assert render_run(fs) == (0, "", "")
    assert set(targets(fs)) == {"O11Y_CONFIG_GRAFANA", "O11Y_CONFIG_LOKI"}
    assert not any(".incoming-" in str(p) for p in fs.modes)
    assert not any(p.name == "stale" for p in fs.modes)


@pytest.mark.parametrize("operation", ["copy", "manifest"])
def test_render_disappearing_file(operation: str) -> None:
    class Disappearing(Fake):
        @override
        def lstat(self, path: Path) -> os.stat_result | None:
            return None

    fs = Disappearing(modes={Path("/tree/file"): stat.S_IFREG})
    if operation == "copy":
        checked = model.check(RENDER_ENV)
        assert isinstance(checked, model.Settings)
        with pytest.raises(FileNotFoundError):
            command._copy_tree(fs, Path("/tree"), Path("/target"), checked, ([], []))
    else:
        with pytest.raises(FileNotFoundError):
            command.tree_files(fs, Path("/tree"))


def test_manifest_tree_ignores_special_entries() -> None:
    fs = Fake(modes={Path("/tree/pipe"): stat.S_IFIFO})
    assert command.tree_files(fs, Path("/tree")) == {}


@pytest.mark.parametrize("early", [False, True])
def test_render_missing_config_entry(early: bool) -> None:
    class Missing(Fake):
        seen = False

        @override
        def list_dir(self, path: Path) -> tuple[str, ...]:
            names = super().list_dir(path)
            if path == SOURCE / "config":
                if early or self.seen:
                    return (*names, "missing")
                self.seen = True
            return names

    fs = Missing()
    render_fixture(fs)
    if early:
        fs.calls.clear()
        assert render_run(fs) == (
            1,
            "",
            "tbhbops o11y render: render: ValidateSource . failed: FileNotFoundError\n",
        )
        assert fs.calls == []
    else:
        assert render_run(fs) == (0, "", "")


def test_render_uses_system_cwd_for_relative_paths() -> None:
    fs = Fake()
    render_fixture(fs, Path("/work/stack"))
    system = replace(world(fs), cwd=Path("/work"), env=RENDER_ENV)
    out, err = io.StringIO(), io.StringIO()
    assert (
        cli.run(
            ["o11y", "render", "--source", "stack", "--out", "a/../output"],
            system,
            out,
            err,
        )
        == 0
    )
    assert out.getvalue() == err.getvalue() == ""
    assert (
        "O11Y_CONFIG_LOKI=/work/output/config/loki-"
        in fs.files[Path("/work/output/release/.env")]
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(0o001, 0o755), (0o010, 0o755), (0o100, 0o755), (0o002, 0o644), (0o600, 0o644)],
)
def test_config_executable_permissions(mode: int, expected: int) -> None:
    fs = Fake()
    render_fixture(fs)
    fs.modes[SOURCE / "config/loki/executable"] = stat.S_IFREG | mode
    fs.files[SOURCE / "config/loki/executable"] = "@O11Y_DISK_RESERVE_GB@"
    assert render_run(fs) == (0, "", "")
    path = Path(targets(fs)["O11Y_CONFIG_LOKI"]) / "executable"
    assert stat.S_IMODE(fs.modes[path]) == expected


def test_render_missing_diagnostics_have_exact_messages() -> None:
    assert render_run(Fake(), {}) == (
        1,
        "",
        (
            "tbhbops o11y render: O11Y_DATA_DIR is not set\n"
            "O11Y_GRAFANA_ROOT_URL is not set\n"
            "O11Y_GRAFANA_OWNER_LOGIN is not set\n"
        ),
    )


def test_render_parser_paths() -> None:
    args = cli.parser().parse_args(
        [
            "o11y",
            "render",
            "--source",
            "/source",
            "--out",
            "/out",
            "--config-root",
            "/config",
        ]
    )
    assert args.source == Path("/source")
    assert args.out == Path("/out")
    assert args.config_root == Path("/config")


@pytest.mark.parametrize("name", ["compose.yaml", "compose.tools.yaml"])
@pytest.mark.parametrize("directory", [False, True])
def test_render_missing_compose_before_writing(name: str, directory: bool) -> None:
    fs = Fake()
    render_fixture(fs)
    fs.unlink(SOURCE / name)
    if directory:
        fs.mkdir(SOURCE / name)
    fs.calls.clear()
    assert render_run(fs) == (
        1,
        "",
        f"tbhbops o11y render: render: source is missing {name}\n",
    )
    assert fs.calls == []


@pytest.mark.parametrize("value", ["", "sensitive-port"])
def test_render_postgres_before_writing(value: str) -> None:
    fs = Fake()
    assert render_run(fs, RENDER_ENV | {"O11Y_POSTGRES_PORT": value}) == (
        1,
        "",
        (
            "tbhbops o11y render: O11Y_POSTGRES_PORT is no longer supported; "
            "PostgreSQL moved out of the stack\n"
        ),
    )
    assert fs.calls == []


@pytest.mark.parametrize(
    "relative", [".tmp.compose.yaml", "config/loki/.tmp.config.yaml"]
)
def test_render_temp_source_before_writing(relative: str) -> None:
    fs = Fake()
    render_fixture(fs)
    fs.write_atomic(SOURCE / relative, "collision", 0o600)
    fs.calls.clear()
    assert render_run(fs) == (
        1,
        "",
        (
            f"tbhbops o11y render: render: source temporary file {relative!r} "
            "is not supported\n"
        ),
    )
    assert fs.calls == []


def test_render_refuses_symlinked_config_directory() -> None:
    fs = Fake()
    render_fixture(fs)
    fs.modes[SOURCE / "config/linked"] = stat.S_IFLNK
    fs.calls.clear()
    assert render_run(fs) == (
        1,
        "",
        (
            "tbhbops o11y render: render: source symlink 'config/linked' "
            "is not supported\n"
        ),
    )
    assert fs.calls == []


def test_render_includes_arbitrary_config_directories() -> None:
    fs = Fake()
    render_fixture(fs)
    fs.write_atomic(SOURCE / "compose.init.yaml", "unused", 0o644)
    fs.mkdir(SOURCE / "config/postgres")
    fs.write_atomic(SOURCE / "config/postgres/file", "ordinary", 0o644)
    assert render_run(fs) == (0, "", "")
    target = Path(targets(fs)["O11Y_CONFIG_POSTGRES"])
    assert fs.files[target / "file"] == "ordinary"
    assert OUTPUT / "release/compose.init.yaml" not in fs.files


def test_render_symlinked_parent_and_inner_symlink(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    real = root / "real"
    real.mkdir()
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)
    fs = o11y.LocalFilesystem()
    source = root / "source"
    render_fixture(fs, source)
    output = link / "nested/output"
    assert render_run(fs, source=source, output=output) == (0, "", "")
    assert fs.root == real / "nested/output"
    assert fs.list_dir(real / "nested/output") == ("config", "release")
    assert str(real / "nested/output/config") in (output / "release/.env").read_text()
    # Output components remain protected after resolving the trusted root.
    for child in (output / "release").iterdir():
        child.unlink()
    (output / "release").rmdir()
    (output / "release").symlink_to(real, target_is_directory=True)
    assert render_run(fs, source=source, output=output) == (
        1,
        "",
        "tbhbops o11y render: render: WriteRelease release failed: OSError\n",
    )
    (output / "release").unlink()
    assert render_run(fs, source=source, output=output) == (0, "", "")
    (output / "config").rename(output / "saved")
    (output / "config").symlink_to(real, target_is_directory=True)
    assert render_run(fs, source=source, output=output) == (
        1,
        "",
        ("tbhbops o11y render: render: EnsureDir config failed: NotADirectoryError\n"),
    )
    assert not (real / ".env").exists()


@pytest.mark.parametrize("failure", ["copy", "chmod", "hash", "manifest", "rename"])
@pytest.mark.parametrize("source_manifest", [False, True])
def test_render_failure_cleans_exact_incoming_files(
    failure: str, source_manifest: bool
) -> None:
    incoming = OUTPUT / "config/.incoming-grafana"

    class Failing(Fake):
        @override
        def read_bytes(self, path: Path) -> bytes:
            if (failure == "copy" and path == SOURCE / "config/grafana/rules.yaml") or (
                failure == "hash" and path == incoming / "rules.yaml"
            ):
                message = "sensitive value"
                raise PermissionError(message)
            return super().read_bytes(path)

        @override
        def chmod(self, path: Path, mode: int) -> None:
            if failure == "chmod" and path == incoming:
                message = "sensitive value"
                raise ValueError(message)
            super().chmod(path, mode)

        @override
        def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None:
            if failure == "manifest" and path == incoming / "MANIFEST.sha256":
                message = "sensitive value"
                raise PermissionError(message)
            super().write_atomic(path, text, mode)

        @override
        def rename(self, source: Path, target: Path) -> None:
            if failure == "rename":
                message = "sensitive value"
                raise PermissionError(message)
            super().rename(source, target)

    fs = Failing()
    render_fixture(fs)
    if source_manifest:
        fs.write_atomic(SOURCE / "config/grafana/MANIFEST.sha256", "old", 0o644)
    code, out, err = render_run(fs)
    exc = "ValueError" if failure == "chmod" else "PermissionError"
    assert (code, out, err) == (
        1,
        "",
        f"tbhbops o11y render: render: RenderConfig config/grafana failed: {exc}\n",
    )
    assert not any(path == incoming or incoming in path.parents for path in fs.modes)
    assert fs.files[SOURCE / "config/grafana/rules.yaml"].startswith("@O11Y_")


def test_render_output_creation_failure_is_private() -> None:
    class Failing(Fake):
        @override
        def prepare_output(self, path: Path) -> Path:
            message = "sensitive value"
            raise PermissionError(message)

    fs = Failing()
    render_fixture(fs)
    assert render_run(fs) == (
        1,
        "",
        "tbhbops o11y render: render: PrepareOutput . failed: PermissionError\n",
    )


def test_render_failure_after_atomic_write_cleans_incoming(tmp_path: Path) -> None:
    class Failing(o11y.LocalFilesystem):
        @override
        def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None:
            super().write_atomic(path, text, mode)
            if path.name == "rules.yaml" and path.parent.name == ".incoming-grafana":
                raise OSError

    fs = Failing()
    root = tmp_path.resolve()
    source, output = root / "source", root / "output"
    render_fixture(fs, source)
    assert render_run(fs, source=source, output=output) == (
        1,
        "",
        "tbhbops o11y render: render: RenderConfig config/grafana failed: OSError\n",
    )
    assert not tuple((output / "config").iterdir())
    assert not tuple(output.rglob(".tmp.*"))
    assert (source / "config/grafana/rules.yaml").exists()


def test_render_opens_only_from_resolved_root_downward(tmp_path: Path) -> None:
    fs = o11y.LocalFilesystem()
    base = tmp_path.resolve()
    ancestor = base / "ancestor"
    root = ancestor / "out"
    mask = os.umask(0)
    try:
        assert fs.prepare_output(root) == root
    finally:
        os.umask(mask)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    # Introduce a symlink above the trusted root after its one resolution.
    # Every render operation must still open directly from that root.
    ancestor.rename(base / "moved")
    ancestor.symlink_to(base / "moved", target_is_directory=True)
    fs.mkdir(root / "nested")
    path = root / "nested/file"
    fs.write_atomic(path, "content", 0o600)
    assert fs.read_text(path) == "content"
    assert fs.read_bytes(path) == b"content"
    assert fs.lstat(path) == path.lstat()
    fs.chown(path, path.stat().st_uid, path.stat().st_gid)
    fs.chmod(path, 0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert fs.list_dir(root / "nested") == ("file",)
    fs.rename(path, root / "renamed")
    assert not path.exists()
    assert (root / "renamed").read_text() == "content"
    fs.unlink(root / "renamed")
    fs.rmdir(root / "nested")
    assert not tuple(root.iterdir())
    assert fs.root == root
    # Init's filesystem still refuses a symlinked ancestor.
    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        o11y.LocalFilesystem().list_dir(root)


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("EnsureDir", "config"),
        ("CopyCompose", "release/compose.yaml"),
        ("CopyCompose", "release/compose.tools.yaml"),
        ("ReadConfigs", "config"),
    ],
)
def test_render_io_error_identifies_step_and_path(kind: str, relative: str) -> None:
    class Failing(Fake):
        scans = 0

        @override
        def mkdir(self, path: Path) -> None:
            if kind == "EnsureDir" and path == OUTPUT / "config":
                raise PermissionError
            super().mkdir(path)

        @override
        def list_dir(self, path: Path) -> tuple[str, ...]:
            if path == SOURCE / "config":
                self.scans += 1
                if kind == "ReadConfigs" and self.scans == 2:
                    raise PermissionError
            return super().list_dir(path)

    fs = Failing()
    render_fixture(fs)
    if kind == "CopyCompose":
        fs.denied.add(SOURCE / Path(relative).name)
    assert render_run(fs) == (
        1,
        "",
        f"tbhbops o11y render: render: {kind} {relative} failed: PermissionError\n",
    )


def validation_panels(names: tuple[str, ...]) -> list[model_validate.Document]:
    memory = [
        {"expr": f'process_resident_memory_bytes{{job="o11y/{name}"}} / 805306368'}
        for name in names
        if name != "rustfs"
    ]
    memory.append(
        {
            "expr": (
                "max(rustfs_system_process_resident_memory_bytes"
                '{job="o11y/rustfs"}) / 805306368'
            )
        }
    )
    samples = [
        {"expr": (f'scrape_samples_post_metric_relabeling{{job="o11y/{name}"}} / 3000')}
        for name in names
        if name != "rustfs"
    ]
    return [
        {
            "id": 1,
            "title": "Resident memory against mem_limit",
            "datasource": {"uid": "mimir"},
            "targets": memory,
        },
        {
            "id": 2,
            "title": "Scraped samples against sample_limit",
            "datasource": {"uid": "mimir"},
            "targets": samples,
        },
    ]


def validation_fixture() -> Fake:
    fs = Fake()
    scrapes = ""
    services: dict[str, model_validate.Document] = {}
    for name in ("alloy", "grafana", "loki", "mimir", "rustfs", "tempo"):
        services[name] = {"image": f"{name}:pinned", "mem_limit": "768m"}
    services["alloy"].update(
        environment={"GOMEMLIMIT": "600MiB"},
        stop_grace_period="30s",
        command=["run", "--stability.level=experimental"],
        ports=["127.0.0.1:4317:4317"],
    )
    services["grafana"].update(
        environment={"GF_SMTP_PASSWORD__FILE": "/run/o11y/alert_smtp_password"},
        volumes=[
            f"${{O11Y_DATA_DIR:?}}/secrets/{name}:/run/o11y/{name}:ro"
            for name in ("alert_discord_webhook_url", "alert_smtp_password")
        ],
    )
    panels = validation_panels(tuple(services))
    for name in services:
        if name != "rustfs":
            scrapes += (
                f'prometheus.scrape "{name}" {{\n'
                f' targets = [{{"job" = "o11y/{name}"}}]\n'
                " sample_limit = 3000\n label_limit = 40\n"
                ' body_size_limit = "16MiB"\n}\n'
            )
    alloy = (
        scrapes
        + """prometheus.remote_write "mimir" {
 endpoint {
   queue_config {
    capacity = 5000
   }
 }
 wal {}
}
otelcol.receiver.otlp "self" {
 http {}
}
otelcol.exporter.otlphttp "mimir_self" {
 sending_queue {
  sizer = "items"
  queue_size = 12000
 }
}
"""
    )
    content = {
        "compose.yaml": json.dumps({"services": services}),
        "compose.tools.yaml": '{"services":{}}',
        "config/alloy/config.alloy": alloy,
        "config/grafana/dashboards/general/stack-health.json": json.dumps(
            {"uid": "health", "title": "Health", "panels": panels}
        ),
        (
            "config/grafana/provisioning/datasources/datasources.yaml"
        ): '{"datasources":[{"uid":"mimir"}]}',
        "config/grafana/provisioning/alerting/alerting.yaml": (
            "url: $__file{/run/o11y/alert_discord_webhook_url}"
        ),
        "config/grafana/provisioning/alerting/rules.yaml": (
            "rustfs_system_process_resident_memory_bytes"
            '{job="o11y/rustfs"}) / 805306368\n'
        ),
        **{
            f"config/{name}/config.yaml": "valid" for name in ("loki", "tempo", "mimir")
        },
    }
    for name, text in content.items():
        path = SOURCE / name
        for parent in reversed(path.parents):
            if parent not in fs.modes:
                fs.mkdir(parent)
        fs.write_atomic(path, text, 0o644)
    fs.modes[Path("/var/run/docker.sock")] = stat.S_IFSOCK | 0o600
    return fs


STABILITY_LOG = (
    "Error: /etc/alloy/config.alloy:1:1: component "
    '"otelcol.processor.deltatocumulative" is at stability '
    'level "experimental", which is below the minimum '
    'allowed stability level "generally-available". Use '
    "--stability.level command-line flag to enable "
    '"experimental" features\nError: validation failed\n'
)


@dataclass
class ValidationRunner(SubprocessRunner):
    replacements: dict[int, Result] = field(default_factory=dict)
    calls: list[tuple[list[str], dict[str, str], int]] = field(default_factory=list)

    @override
    def run(  # one canned response per Docker operation
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        assert input_data is None
        self.calls.append((list(argv), dict(env), timeout))
        index = len(self.calls)
        if index in self.replacements:
            return self.replacements[index]
        if argv[1:3] == ["context", "show"]:
            return Result(0, "default\n")
        if argv[1:3] == ["context", "inspect"]:
            return Result(0, "unix:///var/run/docker.sock\n")
        if argv[1] == "info":
            return Result(0, "docker-desktop|Docker Desktop\n")
        if argv[1] == "version":
            return Result(0, "1.44\n")
        if argv[-2:] == ["--format", "json"]:
            return Result(
                0,
                json.dumps(
                    {
                        "services": {
                            name: {
                                "image": name + ":pinned",
                                "command": ["run", "--stability.level=experimental"],
                            }
                            for name in ("loki", "tempo", "mimir", "alloy")
                        }
                    }
                ),
            )
        if argv[-2:] == ["validate", "/etc/alloy/config.alloy"]:
            return Result(1, STABILITY_LOG)
        if argv[-1].startswith("/invalid/") or any(
            arg.startswith("-config.file=/invalid/") for arg in argv
        ):
            return Result(
                1,
                (
                    "field not_a_loki_section not found\nfield "
                    "not_a_tempo_section not found\nfield "
                    "not_a_mimir_section not found\ncomponent "
                    '"otelcol.exporter.otlp.missing.input" does not exist'
                ),
            )
        return Result(0)


def validate_run(
    fs: Fake, runner: ValidationRunner, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    system = replace(
        world(fs),
        runner=runner,
        platform="darwin",
        env=env or {"HOME": "/operator", "PATH": "/bin"},
    )
    code = cli.run(["o11y", "validate", "--source", str(SOURCE)], system, out, err)
    return code, out.getvalue(), err.getvalue()


def test_validate_success_and_isolation() -> None:
    fs = validation_fixture()
    runner = ValidationRunner()
    code, out, err = validate_run(
        fs,
        runner,
        {
            "PATH": "/bin",
            "HOME": "/operator",
            "O11Y_DATA_DIR": "sentinel",
            "COMPOSE_FILE": "sentinel",
            "O11Y_COMPOSE": "sentinel",
            "DOCKER_HOST": "sentinel",
            "DOCKER_API_VERSION": "sentinel",
        },
    )
    assert (code, err) == (0, "")
    assert out == (
        (
            "ok static: configuration checks pass\nok local engine: "
            "verified\nok compose model: compose.yaml loads\nok "
            "compose model: compose.tools.yaml loads\n"
        )
        + "".join(
            f"ok {name}: valid accepted, invalid rejected\n"
            for name in ("loki", "tempo", "mimir", "alloy")
        )
        + (
            "ok alloy: otelcol.processor.deltatocumulative is the "
            "only component below generally available\nok alloy: "
            "formatted\n"
        )
    )
    assert not any(
        Path("/temporary") == p or Path("/temporary") in p.parents for p in fs.modes
    )
    assert len(runner.calls) == 17
    for index, (argv, env, timeout) in enumerate(runner.calls):
        assert env == {"PATH": "/bin", "HOME": "/operator"} | (
            {"DOCKER_API_VERSION": "1.44"} if index >= 4 else {}
        )
        if argv[1] == "run":
            assert argv[2:10] == [
                "--rm",
                "--network",
                "none",
                "-e",
                "AWS_ACCESS_KEY_ID=validate",
                "-e",
                "AWS_SECRET_ACCESS_KEY=validate",
                "-v",
            ]
            assert argv[11:13] == ["-v", "/temporary/invalid:/invalid:ro"]
            assert argv[10].startswith("/temporary/config/")
            assert argv[10].endswith(":ro")
            assert timeout == 900
        elif argv[1] == "compose":
            assert argv[2:5] == ["--env-file", "/temporary/release/.env.validate", "-f"]
            assert timeout == 120
        else:
            assert timeout == 60
    writes = [
        c
        for c in fs.calls
        if c[0] == "write"
        and isinstance(c[1], Path)
        and c[1].parent == Path("/temporary/invalid")
    ]
    assert len(writes) == 4
    assert all(c[2] == 0o644 for c in writes)
    assert ("chmod", Path("/temporary/invalid"), 0o755) in fs.calls
    assert {entry[1].name for entry in writes if isinstance(entry[1], Path)} == {
        "config.alloy",
        "loki.yaml",
        "tempo.yaml",
        "mimir.yaml",
    }
    assert [argv[5:] for argv, _, _ in runner.calls if argv[1] == "compose"] == [
        ["/temporary/release/compose.yaml", "config", "-q"],
        ["/temporary/release/compose.tools.yaml", "config", "-q"],
        ["/temporary/release/compose.yaml", "config", "--format", "json"],
    ]
    assert runner.calls[13][0][-3:] == [
        "validate",
        "--stability.level=experimental",
        "/etc/alloy/config.alloy",
    ]
    assert runner.calls[14][0][-3:] == [
        "validate",
        "--stability.level=experimental",
        "/invalid/config.alloy",
    ]
    assert runner.calls[-1][0][-3:] == ["fmt", "--test", "/etc/alloy/config.alloy"]


@pytest.mark.parametrize("index", range(1, 18))
def test_validate_every_timeout(index: int) -> None:
    fs = validation_fixture()
    runner = ValidationRunner({index: Result(None)})
    code, out, err = validate_run(fs, runner)
    assert code == 1
    assert err == ""
    part = next(
        part
        for first, last, part in (
            (1, 4, "local engine"),
            (5, 7, "compose model"),
            (8, 9, "loki"),
            (10, 11, "tempo"),
            (12, 13, "mimir"),
            (14, 17, "alloy"),
        )
        if first <= index <= last
    )
    assert f"FAIL {part}: timed out\n" in out
    assert not any(
        Path("/temporary") == p or Path("/temporary") in p.parents for p in fs.modes
    )


@pytest.mark.parametrize(
    ("index", "message"),
    [
        (1, "the Docker context could not be read"),
        (2, "the Docker context could not be inspected"),
        (3, "the engine is not this machine's own"),
        (4, "the Docker API version could not be read"),
        (5, "compose.yaml does not load"),
        (6, "compose.tools.yaml does not load"),
        (7, "could not read model"),
        (8, "the real config was rejected"),
        (9, "the invalid fixture failed without naming what is broken in it"),
        (10, "the real config was rejected"),
        (11, "the invalid fixture failed without naming what is broken in it"),
        (12, "the real config was rejected"),
        (13, "the invalid fixture failed without naming what is broken in it"),
        (14, "the real config was rejected"),
        (15, "the invalid fixture failed without naming what is broken in it"),
        (16, "config.alloy is not formatted"),
    ],
)
def test_validate_failures(index: int, message: str) -> None:
    # Calls 16 and 17 are stability and formatting; valid/invalid checks start at 8.
    if index == 16:
        index = 17
    code, out, err = validate_run(
        validation_fixture(), ValidationRunner({index: Result(1)})
    )
    assert code == 1
    assert err == ""
    part = next(
        part
        for first, last, part in (
            (1, 4, "local engine"),
            (5, 7, "compose model"),
            (8, 9, "loki"),
            (10, 11, "tempo"),
            (12, 13, "mimir"),
            (14, 17, "alloy"),
        )
        if first <= index <= last
    )
    assert f"FAIL {part}: {message}\n" in out


@pytest.mark.parametrize(
    "result",
    [
        Result(0, "bad"),
        Result(0, "{}"),
        Result(0, '{"services":{"alloy":{"command":[]}}}'),
        Result(
            0,
            (
                '{"services":{"alloy":{"command":'
                '["--stability.level=experimental"],"image":3}}}'
            ),
        ),
    ],
)
def test_validate_bad_model(result: Result) -> None:
    code, out, err = validate_run(validation_fixture(), ValidationRunner({7: result}))
    assert code == 1
    assert err == ""
    assert "FAIL compose model: missing images or Alloy --stability.level\n" in out


def test_validate_nonlocal_and_no_api_pin() -> None:
    fs = validation_fixture()
    runner = ValidationRunner({2: Result(0, "tcp://remote:2375")})
    code, out, err = validate_run(fs, runner)
    assert (code, err) == (1, "")
    assert (
        "FAIL local engine: the Docker context does not use "
        "this machine's own daemon socket\n"
    ) in out
    assert len(runner.calls) == 2
    assert "remote" not in out
    fs = validation_fixture()
    runner = ValidationRunner({4: Result(0, "1.43")})
    assert validate_run(fs, runner)[0] == 0
    assert all("DOCKER_API_VERSION" not in env for _, env, _ in runner.calls)


def test_validate_static_and_render_failures() -> None:
    fs = validation_fixture()
    fs.files[SOURCE / "compose.yaml"] = "bad: ["
    runner = ValidationRunner()
    code, out, err = validate_run(fs, runner)
    assert (code, err) == (1, "")
    assert out == "FAIL static: configuration tree could not be read or parsed\n"
    assert runner.calls == []
    fs = validation_fixture()
    fs.files[SOURCE / "config/alloy/config.alloy"] = ""
    code, out, err = validate_run(fs, ValidationRunner())
    assert (code, err) == (1, "")
    assert out.startswith("FAIL static: a scrape lacks")
    fs = Fake()
    code, out, err = validate_run(fs, ValidationRunner())
    assert (code, out, err) == (
        1,
        "FAIL validate: RenderRelease release failed: CliError\n",
        "",
    )


@pytest.mark.parametrize("input_data", [None, "fixture\nwith unicode: \u2603"])
def test_subprocess_runner(
    monkeypatch: pytest.MonkeyPatch, input_data: str | None
) -> None:
    calls = []

    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 3, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake)
    assert SubprocessRunner().run(
        ["docker", "version"], {"PATH": "/bin"}, 900, input_data=input_data
    ) == Result(3, "out", "err")
    assert calls == [
        (
            ["docker", "version"],
            {
                "env": {"PATH": "/bin"},
                "timeout": 900,
                "input": input_data,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "surrogateescape",
                "check": False,
            },
        )
    ]

    def timeout(*_args: object, **_kwargs: object) -> None:
        msg = "docker"
        raise subprocess.TimeoutExpired(msg, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert SubprocessRunner().run([], {}, 1) == Result(None)

    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    assert SubprocessRunner().run([], {}, 1) == Result(127)


def test_temporary_directory_seam(monkeypatch: pytest.MonkeyPatch) -> None:

    calls = []

    def make(**kwargs: object) -> str:
        calls.append(kwargs)
        return "/temporary"

    monkeypatch.setattr(tempfile, "mkdtemp", make)
    assert o11y.LocalFilesystem().temporary_directory() == Path("/temporary")
    assert calls == [{"prefix": "o11y-validate-"}]


@pytest.mark.parametrize(
    ("component", "valid", "invalid"),
    [
        (
            "loki",
            [
                "-config.file=/etc/loki/config.yaml",
                "-config.expand-env=true",
                "-verify-config",
            ],
            [
                "-config.file=/invalid/loki.yaml",
                "-config.expand-env=true",
                "-verify-config",
            ],
        ),
        (
            "tempo",
            [
                "-target=all",
                "-config.file=/etc/tempo/config.yaml",
                "-config.verify=true",
            ],
            ["-target=all", "-config.file=/invalid/tempo.yaml", "-config.verify=true"],
        ),
        (
            "mimir",
            [
                "-config.file=/etc/mimir/config.yaml",
                "-config.expand-env=true",
                "-modules",
            ],
            ["-config.file=/invalid/mimir.yaml", "-config.expand-env=true", "-modules"],
        ),
        (
            "alloy",
            ["validate", "--stability.level=experimental", "/etc/alloy/config.alloy"],
            ["validate", "--stability.level=experimental", "/invalid/config.alloy"],
        ),
    ],
)
def test_validator_arguments(
    component: str, valid: list[str], invalid: list[str]
) -> None:
    assert (
        validation.validator_arguments(
            component, "--stability.level=experimental", fixture=False
        )
        == valid
    )
    assert (
        validation.validator_arguments(
            component, "--stability.level=experimental", fixture=True
        )
        == invalid
    )


def test_image_runner_exact_contract() -> None:
    runner = ValidationRunner()
    env = {"PATH": "/bin", "HOME": "/operator"}
    system = replace(world(Fake()), runner=runner)
    image = validation.Images(
        system,
        env,
        {"loki": Path("/config/loki")},
        Path("/invalid"),
        {"loki": {"image": "loki:pinned"}},
    )
    assert image.run("loki", ["-verify-config"]) == Result(0)
    assert runner.calls == [
        (
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-e",
                "AWS_ACCESS_KEY_ID=validate",
                "-e",
                "AWS_SECRET_ACCESS_KEY=validate",
                "-v",
                "/config/loki:/etc/loki:ro",
                "-v",
                "/invalid:/invalid:ro",
                "loki:pinned",
                "-verify-config",
            ],
            env,
            900,
        )
    ]


def test_local_runner_exact_contract() -> None:
    fs = validation_fixture()
    runner = ValidationRunner()
    system = replace(world(fs), runner=runner, platform="darwin", hostname="operator")
    env = {"HOME": "/operator", "PATH": "/bin"}
    assert validation.local_engine(system, env) is None
    before = {"HOME": "/operator", "PATH": "/bin"}
    assert runner.calls == [
        (["docker", "context", "show"], before, 60),
        (
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                "default",
            ],
            before,
            60,
        ),
        (["docker", "info", "--format", "{{.Name}}|{{.OperatingSystem}}"], before, 60),
        (["docker", "version", "--format", "{{.Server.MinAPIVersion}}"], before, 60),
    ]
    assert env == before | {"DOCKER_API_VERSION": "1.44"}


@pytest.mark.parametrize("endpoint", ["unix:///missing.sock", "unix:///regular.sock"])
def test_local_socket_must_exist_and_be_socket(endpoint: str) -> None:
    fs = validation_fixture()
    fs.modes[Path("/regular.sock")] = stat.S_IFREG | 0o600
    runner = ValidationRunner({2: Result(0, endpoint)})
    code, out, err = validate_run(fs, runner)
    assert (code, err) == (1, "")
    assert "FAIL local engine:" in out
    assert len(runner.calls) == 2


def test_local_platform_identity_and_no_environment() -> None:
    fs = validation_fixture()
    runner = ValidationRunner({3: Result(0, "operator|Ubuntu\n")})
    system = replace(
        world(fs), runner=runner, platform="linux", engine_hostname="operator"
    )
    assert validation.local_engine(system, {}) is None
    runner = ValidationRunner({3: Result(0, "wrong|Ubuntu\n")})
    assert (
        validation.local_engine(replace(system, runner=runner), {})
        == "the engine is not this machine's own"
    )


def test_validate_usage_and_parser(capsys: pytest.CaptureFixture[str]) -> None:
    for args in ((), ("--source",), ("--source", "/source", "--model")):
        with pytest.raises(SystemExit) as exc:
            cli.parser().parse_args(["o11y", "validate", *args])
        assert exc.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["o11y", "validate", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--source SOURCE" in help_text
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["o11y", "--help"])
    assert (
        "validate            check an observability release" in capsys.readouterr().out
    )
    parsed = cli.parser().parse_args(["o11y", "validate", "--source", "relative"])
    assert parsed.group == "o11y"
    assert parsed.command == "validate"
    assert parsed.source == Path("relative")


def test_validate_preserves_explicit_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = command.dispatch
    observations = []

    def render_capture(args: argparse.Namespace, system: cli.System) -> None:
        observations.append((args, system.env))
        original(args, system)
        env = system.filesystem.read_text(Path("/temporary/release/.env.validate"))
        assert "O11Y_S3_ENV_FILE=dummy-s3.env\n" in env
        assert "O11Y_DATA_DIR=/temporary/data\n" in env

    monkeypatch.setattr(command, "dispatch", render_capture)
    assert validate_run(validation_fixture(), ValidationRunner())[0] == 0
    assert observations == [
        (
            argparse.Namespace(source=SOURCE, out=Path("/temporary"), config_root=None),
            {
                "O11Y_DATA_DIR": "/temporary/data",
                "O11Y_GRAFANA_ROOT_URL": "https://grafana.example.example.invalid/",
                "O11Y_GRAFANA_OWNER_LOGIN": "owner@example.invalid",
            },
        )
    ]


def test_model_image_type_checked() -> None:
    model_data: model_validate.Document = {
        "services": {
            name: {
                "image": name + ":pinned",
                "command": ["run", "--stability.level=experimental"],
            }
            for name in ("alloy", "tempo", "mimir", "loki")
        }
    }
    model_data["services"]["tempo"]["image"] = False
    code, out, err = validate_run(
        validation_fixture(), ValidationRunner({7: Result(0, json.dumps(model_data))})
    )
    assert (code, err) == (1, "")
    assert out.endswith(
        "FAIL compose model: missing images or Alloy --stability.level\n"
    )


@pytest.mark.parametrize(
    ("platform", "endpoint", "env"),
    [
        ("linux", "unix:///run/docker.sock", {}),
        ("darwin", "unix:///operator/.docker/run/docker.sock", {"HOME": "/operator"}),
        ("darwin", "unix:///.docker/run/docker.sock", {}),
    ],
)
def test_local_alternative_socket(
    platform: str, endpoint: str, env: dict[str, str]
) -> None:
    fs = validation_fixture()
    fs.modes[Path(endpoint.removeprefix("unix://"))] = stat.S_IFSOCK | 0o600
    runner = ValidationRunner({2: Result(0, endpoint), 3: Result(0, "operator|Ubuntu")})
    if platform == "darwin":
        runner.replacements[3] = Result(0, "docker-desktop|Docker Desktop")
    system = replace(
        world(fs), runner=runner, platform=platform, engine_hostname="operator"
    )
    problem = validation.local_engine(system, env)
    if endpoint == "unix:///.docker/run/docker.sock":
        assert (
            problem
            == "the Docker context does not use this machine's own daemon socket"
        )
    else:
        assert problem is None


def test_fixture_and_stability_stderr() -> None:
    runner = ValidationRunner(
        {
            9: Result(1, "", "field not_a_loki_section not found"),
            16: Result(1, "", STABILITY_LOG),
        }
    )
    assert validate_run(validation_fixture(), runner)[0] == 0
    code, out, err = validate_run(
        validation_fixture(), ValidationRunner({16: Result(0)})
    )
    assert (code, err) == (1, "")
    assert (
        "FAIL alloy: config.alloy passes at the default stability level, "
        "so --stability.level=experimental is not needed\n"
    ) in out


def test_checks_return_boolean_failure() -> None:
    fs = validation_fixture()
    runner = ValidationRunner({5: Result(1)})
    system = replace(world(fs), runner=runner, platform="darwin")
    configs = {
        name: Path("/config") / name for name in ("alloy", "loki", "mimir", "tempo")
    }
    assert (
        validation._checks(  # boolean contract used by dispatch
            system, {}, Path("/release"), configs, Path("/invalid"), io.StringIO()
        )
        is False
    )


def test_validate_equal_sign_in_temporary_path() -> None:
    class EqualPath(Fake):
        @override
        def temporary_directory(self) -> Path:
            path = Path("/temporary=check")
            self.mkdir(path)
            return path

    fs = validation_fixture()
    other = EqualPath(files=fs.files, modes=fs.modes)
    assert validate_run(other, ValidationRunner())[0] == 0
    assert not any(Path("/temporary=check") in p.parents for p in other.modes)


def test_validate_temporary_failure() -> None:
    class Denied(Fake):
        @override
        def temporary_directory(self) -> Path:
            raise PermissionError

    assert validate_run(Denied(), ValidationRunner()) == (
        1,
        "FAIL validate: CreateTemporary directory failed: PermissionError\n",
        "",
    )


def test_validate_nested_dashboard() -> None:
    fs = validation_fixture()
    path = SOURCE / "config/grafana/dashboards/nested"
    fs.mkdir(path)
    fs.write_atomic(
        path / "bad.json",
        ('{"uid":"bad","title":"Bad","variables":[{"datasource":{"uid":"unknown"}}]}'),
        0o644,
    )
    code, out, err = validate_run(fs, ValidationRunner())
    assert (code, out, err) == (
        1,
        "FAIL static: nested/bad.json: datasource uid unknown does not exist\n",
        "",
    )


def test_temporary_directory_resolves_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    target = root / "target"
    target.mkdir()
    link = root / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    fs = o11y.LocalFilesystem()
    temporary = fs.temporary_directory()
    assert temporary.parent == target
    assert temporary == temporary.resolve()
    assert all(not part.is_symlink() for part in (temporary, *temporary.parents))
    command.remove_tree(fs, temporary)
    assert not temporary.exists()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize(
    "exception", [PermissionError, ValueError, CliError, RuntimeError]
)
def test_validate_cleanup_failure_preserves_result(
    failed: bool, exception: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = validation_fixture()

    def fail(path: Path) -> os.stat_result | None:
        assert path == Path("/temporary")
        message = "sensitive exception text"
        raise exception(message)

    original = command.dispatch

    # Only fail the root lookup, leaving static reads and fixture writes intact.
    original_lstat = fs.lstat

    def deny_root(path: Path) -> os.stat_result | None:
        if path == Path("/temporary"):
            return fail(path)
        return original_lstat(path)

    def render_then_deny(args: argparse.Namespace, system: cli.System) -> None:
        original(args, system)
        monkeypatch.setattr(fs, "lstat", deny_root)

    monkeypatch.setattr(command, "dispatch", render_then_deny)
    if failed:
        fs.files[SOURCE / "config/alloy/config.alloy"] = ""
    code, out, err = validate_run(fs, ValidationRunner())
    assert (code, err) == (1, "")
    assert out.endswith(
        "FAIL validate: temporary directory '/temporary' could not be removed\n"
    )
    assert out.count("could not be removed") == 1
    assert out.startswith("FAIL static:" if failed else "ok static:")


@pytest.mark.parametrize(
    ("step", "item", "attribute"),
    [
        ("RenderRelease", "release", "dispatch"),
        ("ReadEnvironment", "release/.env", "read_text"),
        ("ReadConfigs", "release/.env", "Path"),
        ("PrepareFixtures", "invalid", "_fixtures"),
        ("RunChecks", "docker", "_checks"),
    ],
)
def test_validate_failed_steps(
    step: str, item: str, attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = validation_fixture()

    def fail(*_args: object, **_kwargs: object) -> None:
        message = "sensitive exception text"
        raise ValueError(message)

    if attribute == "dispatch":
        monkeypatch.setattr(command, attribute, fail)
    elif attribute == "read_text":
        original = command.dispatch

        def render_then_deny(args: argparse.Namespace, system: cli.System) -> None:
            original(args, system)
            fs.denied.add(Path("/temporary/release/.env"))

        monkeypatch.setattr(command, "dispatch", render_then_deny)
    else:
        monkeypatch.setattr(validation, attribute, fail)
    code, out, err = validate_run(fs, ValidationRunner())
    exception = "PermissionError" if attribute == "read_text" else "ValueError"
    assert (code, err) == (1, "")
    assert out.endswith(f"FAIL validate: {step} {item} failed: {exception}\n")
    assert "sensitive" not in out
    assert Path("/temporary") not in fs.modes


def test_validate_real_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    source = root / "source"
    repo = next(
        parent
        for parent in Path(__file__).parents
        if (parent / "o11y/compose.yaml").is_file()
    )
    shutil.copytree(repo / "o11y", source)
    target = root / "target"
    target.mkdir()
    link = root / "tmp"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))

    @dataclass
    class Runner(SubprocessRunner):
        calls: list[list[str]] = field(default_factory=list)

        @override
        def run(
            self,
            argv: Sequence[str],
            env: Mapping[str, str],
            timeout: int,
            *,
            input_data: str | None = None,
        ) -> Result:
            assert input_data is None
            self.calls.append(list(argv))
            assert timeout == validation.METADATA_TIMEOUT
            assert env["TMPDIR"] == str(link)
            temporary = next(target.iterdir())
            assert temporary == temporary.resolve()
            release = temporary / "release"
            assert (release / ".env").is_file()
            assert (release / ".env.validate").is_file()
            assert (release / "dummy-s3.env").is_file()
            assert (release / "compose.yaml").is_file()
            assert (release / "compose.tools.yaml").is_file()
            for component in validation.COMPONENTS:
                name = "config.alloy" if component == "alloy" else f"{component}.yaml"
                assert (temporary / "invalid" / name).read_bytes() == (
                    Path(validation.__file__).parent / "invalid" / name
                ).read_bytes()
            return Result(0)

    runner = Runner()
    system = replace(
        cli.real_system(),
        filesystem=o11y.LocalFilesystem(),
        runner=runner,
        cwd=root,
        env={"TMPDIR": str(link)},
    )
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(["o11y", "validate", "--source", str(source)], system, out, err)
    assert (code, err.getvalue()) == (1, "")
    assert out.getvalue() == (
        "ok static: configuration checks pass\n"
        "FAIL local engine: the Docker context does not use "
        "this machine's own daemon socket\n"
    )
    assert runner.calls == [
        ["docker", "context", "show"],
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", ""],
    ]
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("index", [8, 17])
@pytest.mark.parametrize("exit_code", [124, 125, 126, 127, 137, -9])
def test_real_config_launch_failure(index: int, exit_code: int) -> None:
    code, out, err = validate_run(
        validation_fixture(), ValidationRunner({index: Result(exit_code)})
    )
    assert (code, err) == (1, "")
    if exit_code < 0:
        message = "the real config check was killed by a signal"
    elif exit_code >= 125:
        message = f"the real config check never started (exit {exit_code})"
    else:
        message = (
            "the real config was rejected"
            if index == 8
            else "config.alloy is not formatted"
        )
    part = "loki" if index == 8 else "alloy"
    assert f"FAIL {part}: {message}\n" in out


@pytest.mark.parametrize("mode", [None, stat.S_IFLNK, stat.S_IFREG])
def test_local_default_socket_refusals(mode: int | None) -> None:
    fs = validation_fixture()
    path = Path("/var/run/docker.sock")
    if mode is None:
        del fs.modes[path]
    else:
        fs.modes[path] = mode | 0o600
    runner = ValidationRunner()
    code, out, err = validate_run(fs, runner)
    assert (code, err) == (1, "")
    assert (
        "FAIL local engine: the Docker context does not use "
        "this machine's own daemon socket\n" in out
    )
    assert len(runner.calls) == 2


def test_local_socket_symlinked_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    home = tmp_path / "home"
    home.symlink_to(actual, target_is_directory=True)
    directory = actual / ".docker/run"
    directory.mkdir(parents=True)
    path = home / ".docker/run/docker.sock"
    monkeypatch.chdir(directory)
    if sys.platform != "win32":
        with socket.socket(socket.AF_UNIX) as daemon:
            daemon.bind("docker.sock")
            fs = o11y.LocalFilesystem()
            assert fs.socket_lstat(path) == (directory / "docker.sock").lstat()
            runner = ValidationRunner({2: Result(0, f"unix://{path}")})
            system = replace(
                world(Fake()), filesystem=fs, runner=runner, platform="darwin"
            )
            assert validation.local_engine(system, {"HOME": str(home)}) is None


# RustFS probe host orchestration; all engine calls below are fakes.


@dataclass
class ProbeHandle:
    events: list[object]
    code: int | None = 0
    killed: bool = False
    failure: bool = False

    def wait(self, timeout: int) -> int | None:
        self.events.append(("wait", timeout))
        return -9 if self.killed else self.code

    def kill(self) -> None:
        self.events.append("kill-writer")
        self.killed = True
        if self.failure:
            raise RuntimeError


@dataclass
class ProbeRunner(ValidationRunner):
    fs: o11y.Filesystem = field(default_factory=Fake)
    identity: str = "native|Ubuntu"
    failure: str = ""
    result: Result = field(default_factory=lambda: Result(1, "private", "private"))
    events: list[object] = field(default_factory=list)
    handle: ProbeHandle = field(init=False)
    writing: str = "probe: writing\nprobe: 5 acknowledged writes survived\nprivate"

    def __post_init__(self) -> None:
        self.handle = ProbeHandle(self.events)

    @override
    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        assert input_data is None
        self.events.append(list(argv))
        text = " ".join(argv)
        if self.failure and self.failure in text:
            self.calls.append((list(argv), dict(env), timeout))
            return self.result
        if argv[-2:] == ["--format", "json"]:
            self.calls.append((list(argv), dict(env), timeout))
            return Result(0, '{"services":{"rustfs":{"image":"rustfs:pinned"}}}')
        if argv[1] == "info":
            self.calls.append((list(argv), dict(env), timeout))
            return Result(0, self.identity)
        if "logs" in argv:
            self.calls.append((list(argv), dict(env), timeout))
            return Result(0, '{"console_enabled":false}')
        if "exec" in argv:
            self.calls.append((list(argv), dict(env), timeout))
            return Result(0, "200\n") if "9000" in text else Result(7)
        return super().run(argv, env, timeout)

    @override
    def start(
        self, argv: Sequence[str], env: Mapping[str, str], stdout_path: Path
    ) -> ProbeHandle:
        assert stdout_path == Path("/temporary/crash.log")
        assert self.fs.read_text(stdout_path) == ""
        info = self.fs.lstat(stdout_path)
        assert info is not None
        assert info.st_mode & 0o777 == 0o600
        self.events.append(list(argv))
        self.calls.append((list(argv), dict(env), -1))
        self.fs.write_atomic(stdout_path, self.writing, 0o600)
        return self.handle


@dataclass
class ProbeClock:
    now: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def probe_world(fs: Fake, runner: ProbeRunner, clock: ProbeClock) -> cli.System:
    return replace(
        world(fs, 123),
        egid=456,
        engine_hostname="native",
        runner=runner,
        platform="linux",
        env={
            "HOME": "/operator",
            "PATH": "/bin",
            "DOCKER_HOST": "private",
            "AWS_SECRET_ACCESS_KEY": "private",
            "O11Y_DATA_DIR": "private",
        },
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def probe_run(system: cli.System) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    result = cli.run(
        ["o11y", "rustfs-probe", "--source", str(SOURCE)], system, out, err
    )
    return result, out.getvalue(), err.getvalue()


def test_probe_success_and_contract() -> None:
    fs = validation_fixture()
    runner, clock = ProbeRunner(fs=fs), ProbeClock()
    code, out, err = probe_run(probe_world(fs, runner, clock))
    assert (code, err) == (0, "")
    assert out == (
        "engine mode: local\n"
        "ok 1: start log is clean and reports the console off\n"
        "ok 2: ready on 9000 and nothing listens on 9001\n"
        "ok 3: a request signed with the default login is refused\n"
        "ok 4: 1 KiB, 10 MiB and multipart 100 MiB round trips match, "
        "1,100 keys list across pages, delete empties the bucket\n"
        "ok 5: 5 acknowledged writes survived a kill and restart, "
        "and every listed key is readable\n"
        "ok 6: probe bucket removed\n"
    )
    assert clock.sleeps == [2]
    prefix = [
        "docker",
        "compose",
        "-p",
        "o11y-probe",
        "--env-file",
        "/temporary/release/.env",
        "-f",
        "/temporary/release/compose.yaml",
        "-f",
        "/temporary/release/compose.tools.yaml",
    ]
    utility = [
        "docker",
        "run",
        "--rm",
        "--user",
        "0:0",
        "--network",
        "none",
        "-v",
        "/temporary:/d",
        "--entrypoint",
    ]
    expected = [
        (["docker", "context", "show"], 60),
        (
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                "default",
            ],
            60,
        ),
        (["docker", "info", "--format", "{{.Name}}|{{.OperatingSystem}}"], 60),
        (["docker", "version", "--format", "{{.Server.MinAPIVersion}}"], 60),
        (
            [
                "docker",
                "compose",
                "--env-file",
                "/temporary/release/.env.validate",
                "-f",
                "/temporary/release/compose.yaml",
                "config",
                "--format",
                "json",
            ],
            120,
        ),
        ([*prefix, "down", "-v", "--remove-orphans"], 120),
        (
            [*utility, "chown", "rustfs:pinned", "-R", "10001:10001", "/d/data/rustfs"],
            120,
        ),
        *[
            (
                [
                    *utility,
                    "chown",
                    "rustfs:pinned",
                    f"{secret.uid}:{secret.gid}",
                    f"/d/data/secrets/{secret.name}",
                ],
                120,
            )
            for secret in init.SECRETS
            if secret.guard is not None and secret.guard.startswith("rustfs/")
        ],
        ([*prefix, "up", "-d", "rustfs"], 120),
        (
            [
                *prefix,
                "exec",
                "-T",
                "rustfs",
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-m",
                "10",
                "http://127.0.0.1:9000/health/ready",
            ],
            15,
        ),
        ([*prefix, "logs", "--no-color", "rustfs"], 120),
        (
            [
                *prefix,
                "exec",
                "-T",
                "rustfs",
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-m",
                "5",
                "http://127.0.0.1:9001/",
            ],
            15,
        ),
        ([*prefix, "run", "--rm", "-T", "s3probe", "default-credentials"], 900),
        ([*prefix, "run", "--rm", "-T", "s3probe", "objects"], 900),
        (
            [
                *prefix,
                "run",
                "--rm",
                "-T",
                "--name",
                "o11y-probe-writer",
                "s3probe",
                "crash",
            ],
            -1,
        ),
        ([*prefix, "kill", "rustfs"], 120),
        ([*prefix, "start", "rustfs"], 120),
        ([*prefix, "run", "--rm", "-T", "s3probe", "cleanup"], 900),
        (["docker", "rm", "-f", "o11y-probe-writer"], 120),
        ([*prefix, "down", "-v", "--remove-orphans"], 120),
        ([*utility, "rm", "rustfs:pinned", "-rf", "/d/data"], 120),
    ]
    assert [(a, t) for a, _, t in runner.calls] == expected
    for index, (_, env, _) in enumerate(runner.calls):
        allowed = {"HOME": "/operator", "PATH": "/bin"}
        if index >= 4:
            allowed["DOCKER_API_VERSION"] = "1.44"
        if index >= 5:
            allowed["O11Y_S3_ENV_FILE"] = "/temporary/data/secrets/s3.env"
        assert env == allowed
    assert runner.events[-5:] == [
        "kill-writer",
        ("wait", 120),
        expected[-3][0],
        expected[-2][0],
        expected[-1][0],
    ]
    assert ("wait", 600) in runner.events
    assert Path("/temporary") not in fs.modes
    assert {
        fs.owners[p] for p in fs.owners if Path("/temporary/data") in p.parents
    } == {(123, 456)}


@pytest.mark.parametrize(
    ("operation", "step", "rule"),
    [
        ("up -d", 1, "rustfs did not start"),
        (
            "logs",
            1,
            "start log unavailable",
        ),
        ("9000/health/ready", 2, "rustfs did not become ready"),
        ("9001/", 2, "something answers on the console port"),
        ("default-credentials", 3, "the default login was not refused"),
        ("s3probe objects", 4, "object round trips failed"),
        ("kill rustfs", 5, "rustfs could not be killed"),
        ("start rustfs", 5, "rustfs did not restart"),
        ("s3probe cleanup", 6, "the probe bucket could not be removed"),
    ],
)
@pytest.mark.parametrize("status", [1, None])
def test_probe_early_exit_cleans_every_resource(
    operation: str, step: int, rule: str, status: int | None
) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(
        fs=fs, failure=operation, result=Result(status, "private", "private")
    )
    code, out, err = probe_run(probe_world(fs, runner, ProbeClock()))
    assert (code, err) == (1, "")
    assert out.endswith(f"FAIL {step}: {rule}\n")
    assert "private" not in out
    assert f"ok {step}:" not in out
    assert Path("/temporary") not in fs.modes
    calls = [a for a, _, _ in runner.calls]
    assert calls[-3] == ["docker", "rm", "-f", "o11y-probe-writer"]
    assert calls[-2][-3:] == ["down", "-v", "--remove-orphans"]
    assert calls[-1][-4:] == ["rm", "rustfs:pinned", "-rf", "/d/data"]
    if step >= 5:
        assert runner.events[-5:-3] == ["kill-writer", ("wait", 120)]


@pytest.mark.parametrize("writing", ["", "probe: writing\nprivate"])
@pytest.mark.parametrize("exit_code", [0, 1, None])
def test_probe_writer_rules(writing: str, exit_code: int | None) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs, writing=writing)
    runner.handle.code = exit_code
    clock = ProbeClock()
    code, out, err = probe_run(probe_world(fs, runner, clock))
    assert (code, err) == (1, "")
    rule = (
        "an acknowledged write was lost or a listed key is unreadable after a kill"
        if writing
        else "the writer never started"
    )
    assert out.endswith(f"FAIL 5: {rule}\n")
    assert "private" not in out
    assert runner.handle.killed
    assert Path("/temporary") not in fs.modes
    if not writing:
        assert clock.sleeps == [2] * 60


@pytest.mark.parametrize("output", ["", "201", "200\nprivate", "200 ", "200X"])
def test_probe_readiness_requires_exact_200(output: str) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs, failure="9000/health/ready", result=Result(0, output))
    code, out, err = probe_run(probe_world(fs, runner, ProbeClock()))
    assert (code, err) == (1, "")
    assert out.endswith("FAIL 2: rustfs did not become ready\n")


@pytest.mark.parametrize(
    "result",
    [
        Result(0, ""),
        Result(0, "private"),
        Result(0, "{}"),
        Result(0, '{"services":{"rustfs":{"image":0}}}'),
        Result(0, '{"services":{"rustfs":{"image":""}}}'),
        Result(1),
        Result(None),
    ],
)
def test_probe_model_failures(result: Result) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs, failure="config --format json", result=result)
    code, out, err = probe_run(probe_world(fs, runner, ProbeClock()))
    assert (code, err) == (1, "")
    rule = "could not read RustFS image" if result.code == 0 else "could not read model"
    assert out.startswith(f"engine mode: local\nFAIL {rule}\n")
    assert not any("chown" in a or "/d/data" in a for a, _, _ in runner.calls)
    assert Path("/temporary") not in fs.modes


@pytest.mark.parametrize("operation", ["chown", "down -v"])
def test_probe_preparation_failure(operation: str) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs, failure=operation)
    code, out, err = probe_run(probe_world(fs, runner, ProbeClock()))
    assert (code, err) == (1, "")
    rule = (
        "init failed"
        if operation == "chown"
        else "leftover project could not be removed"
    )
    assert out.startswith(f"engine mode: local\nFAIL {rule}\n")
    assert "ok 1:" not in out
    assert Path("/temporary") not in fs.modes


def test_probe_local_guard_precedes_all_work() -> None:
    fs = validation_fixture()
    before = dict(fs.files)
    runner = ProbeRunner(fs=fs, failure="context show")
    code, out, err = probe_run(probe_world(fs, runner, ProbeClock()))
    assert (code, err) == (1, "")
    assert (
        out == "engine mode: local\n"
        "FAIL local engine: the Docker context could not be read\n"
    )
    assert fs.files == before
    assert len(runner.calls) == 1


@pytest.mark.parametrize("operation", ["docker rm -f", "down -v", "--entrypoint rm"])
@pytest.mark.parametrize("raises", [False, True])
def test_probe_cleanup_attempts_every_call(
    operation: str, raises: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs)
    original = runner.run

    def run_call(argv: Sequence[str], env: Mapping[str, str], timeout: int) -> Result:
        if operation in " ".join(argv):
            runner.events.append(list(argv))
            if raises:
                msg = "private"
                raise RuntimeError(msg)
            return Result(None, "private")
        return original(argv, env, timeout)

    probe = rustfs_probe.Probe(
        probe_world(fs, runner, ProbeClock()),
        {},
        Path("/temporary"),
        linux=True,
        image="rustfs:pinned",
    )
    fs.mkdir(Path("/temporary"))
    probe.writer = runner.handle
    monkeypatch.setattr(runner, "run", run_call)
    assert probe.cleanup() is False
    assert runner.events[:2] == ["kill-writer", ("wait", 120)]
    assert len(runner.events) == 5
    assert Path("/temporary") not in fs.modes


@pytest.mark.parametrize("kill_failure", [False, True])
def test_probe_cleanup_reaps_after_kill_failure(kill_failure: bool) -> None:
    fs = validation_fixture()
    fs.mkdir(Path("/temporary"))
    runner = ProbeRunner(fs=fs)
    runner.handle.failure = kill_failure
    probe = rustfs_probe.Probe(
        probe_world(fs, runner, ProbeClock()), {}, Path("/temporary"), linux=True
    )
    probe.writer = runner.handle
    assert probe.cleanup() is (not kill_failure)
    assert runner.events[:2] == ["kill-writer", ("wait", 120)]
    assert Path("/temporary") not in fs.modes


def test_probe_cleanup_failure_changes_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs, failure="docker rm -f")
    code, out, err = probe_run(probe_world(fs, runner, ProbeClock()))
    assert (code, err) == (1, "")
    assert out.endswith(
        "ok 6: probe bucket removed\n"
        "FAIL cleanup: probe resources could not be removed\n"
    )
    assert out.count("FAIL cleanup:") == 1

    def fail_remove(_fs: o11y.Filesystem, _path: Path) -> None:
        msg = "private"
        raise RuntimeError(msg)

    fs.mkdir(Path("/temporary"))
    probe = rustfs_probe.Probe(
        probe_world(fs, runner, ProbeClock()), {}, Path("/temporary"), linux=True
    )
    monkeypatch.setattr(command, "remove_tree", fail_remove)
    assert probe.cleanup() is False


def test_probe_temporary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs)

    def fail() -> Path:
        msg = "private"
        raise OSError(msg)

    monkeypatch.setattr(fs, "temporary_directory", fail)
    assert probe_run(probe_world(fs, runner, ProbeClock())) == (
        1,
        "engine mode: local\nFAIL probe: temporary directory could not be created\n",
        "",
    )
    assert len(runner.calls) == 4


def test_probe_init_refusal_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fs = validation_fixture()
    system = world(fs, 123)

    def refuse(_facts: init.Facts, _owner: tuple[int, int]) -> init.Refusal:
        return init.Refusal("private")

    monkeypatch.setattr(init, "plan", refuse)
    with pytest.raises(rustfs_probe.ProbeError, match=r"^init failed$"):
        rustfs_probe._initialize(system, Path("/data"))

    def fail(_probe: rustfs_probe.Probe, _source: Path) -> None:
        msg = "private"
        raise ValueError(msg)

    runner = ProbeRunner(fs=fs)
    monkeypatch.setattr(rustfs_probe.Probe, "prepare", fail)
    assert probe_run(probe_world(fs, runner, ProbeClock())) == (
        1,
        "engine mode: local\nFAIL probe: ValueError\n",
        "",
    )


def test_probe_usage(capsys: pytest.CaptureFixture[str]) -> None:
    options: list[list[str]] = [
        [],
        ["--release", "/x"],
        ["--project", "custom"],
        ["--", "up"],
    ]
    for extra in options:
        with pytest.raises(SystemExit) as caught:
            cli.parser().parse_args(["o11y", "rustfs-probe", *extra])
        assert caught.value.code == 2
        assert "usage:" in capsys.readouterr().err
    args = cli.parser().parse_args(["o11y", "rustfs-probe", "--source", "/src"])
    assert args.source == Path("/src")
    with pytest.raises(SystemExit) as caught:
        cli.parser().parse_args(["o11y", "rustfs-probe", "--help"])
    assert caught.value.code == 0
    assert "--source" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["o11y", "--help"])
    help_text = capsys.readouterr().out
    assert "exercise RustFS locally" in help_text
    assert "XX" not in help_text


def test_effective_gid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getegid", lambda: 456)
    assert filesystem.effective_gid() == 456
    monkeypatch.setattr(sys, "platform", "win32")
    assert filesystem.effective_gid() == -1


@pytest.mark.parametrize("linux", [False, True])
def test_probe_real_filesystem_symlink_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux: bool
) -> None:
    root = tmp_path.resolve()
    target = root / "target"
    target.mkdir()
    link = root / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(link))
    monkeypatch.setattr(tempfile, "tempdir", None)
    fs = o11y.LocalFilesystem()
    temporary = fs.temporary_directory()
    assert temporary.parent == target
    assert temporary == temporary.resolve()
    assert temporary.stat().st_mode & 0o777 == 0o700
    source = root / "source"
    render_fixture(fs, source)
    runner = ProbeRunner(fs=fs)
    system = replace(
        cli.real_system(),
        filesystem=fs,
        runner=runner,
        env={"TMPDIR": str(link)},
        choice=lambda charset: charset[0],
    )
    probe = rustfs_probe.Probe(system, {"TMPDIR": str(link)}, temporary, linux=linux)
    probe.prepare(source)
    data = temporary / "data"
    assert all(path.stat().st_mode & 0o002 == 0 for path in data.rglob("*"))
    for secret in init.SECRETS[:2]:
        assert (data / "secrets" / secret.name).stat().st_mode & 0o777 == 0o400
    expected_data = str(data) if linux else "/var/lib/o11y-test/probe"
    assert (
        f"O11Y_DATA_DIR={expected_data}\n" in (temporary / "release/.env").read_text()
    )
    assert (data / "secrets/s3.env").is_file()
    assert (data / "secrets/s3.env").stat().st_uid == system.euid
    assert (data / "secrets/s3.env").stat().st_gid == system.egid
    assert (data / "secrets/s3.env").stat().st_mode & 0o777 == 0o600
    assert (
        "O11Y_GRAFANA_ROOT_URL=http://127.0.0.1:13000/\n"
        in (temporary / "release/.env").read_text()
    )
    assert probe.env["O11Y_S3_ENV_FILE"] == str(data / "secrets/s3.env")
    original = command.remove_tree
    events = runner.events

    def remove(filesystem: o11y.Filesystem, path: Path) -> None:
        if path == temporary:
            events.append("remove-temp")
        original(filesystem, path)

    monkeypatch.setattr(command, "remove_tree", remove)
    probe.writer = runner.handle
    assert probe.cleanup()
    assert events[-6:-4] == ["kill-writer", ("wait", 120)]
    assert events[-4] == ["docker", "rm", "-f", "o11y-probe-writer"]
    assert isinstance(events[-3], list)
    assert events[-3][-3:] == ["down", "-v", "--remove-orphans"]
    assert isinstance(events[-2], list)
    assert events[-2][-4:] == [
        "rm",
        "rustfs:pinned",
        "-rf",
        "/d/data" if linux else "/d/probe",
    ]
    assert events[-1] == "remove-temp"
    assert list(target.iterdir()) == []


def test_background_runner_process_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    launches: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        pid = 123
        running = True
        timed_out = False

        def wait(self, *, timeout: int) -> int:
            calls.append(("wait", timeout))
            if self.timed_out:
                msg = "private"
                raise subprocess.TimeoutExpired(msg, timeout)
            return 17

        def poll(self) -> int | None:
            return None if self.running else 17

        def kill(self) -> None:
            calls.append("windows-kill")

    process = Process()

    def start(argv: Sequence[str], **kwargs: object) -> Process:
        launches.append((list(argv), kwargs))
        return process

    def killpg(pid: int, sig: int) -> None:
        calls.append(("killpg", pid, sig))

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(os, "killpg", killpg)
    output = tmp_path / "writer"
    handle = SubprocessRunner().start(["compose", "crash"], {"PATH": "/bin"}, output)
    assert isinstance(handle, ProcessHandle)
    argv, kwargs = launches[0]
    assert argv == ["compose", "crash"]
    assert kwargs["env"] == {"PATH": "/bin"}
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    stream = kwargs["stdout"]
    assert isinstance(stream, io.BufferedWriter)
    assert stream.name == str(output)
    assert stream.closed
    assert handle.wait(600) == 17
    assert calls[-1] == ("wait", 600)
    process.timed_out = True
    assert handle.wait(120) is None
    assert calls[-1] == ("wait", 120)
    monkeypatch.setattr(sys, "platform", "linux")
    handle.kill()
    assert calls[-1] == ("killpg", 123, 9)
    monkeypatch.setattr(sys, "platform", "win32")
    handle.kill()
    assert calls[-1] == "windows-kill"
    monkeypatch.setattr(sys, "platform", "linux")
    process.running = False
    assert process.poll() == 17
    previous = list(calls)
    handle.kill()
    assert calls == [*previous, ("killpg", 123, 9)]

    def vanished(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", vanished)
    handle.kill()


@pytest.mark.parametrize(
    ("stdout", "stderr", "passed"),
    [
        ("private", "", False),
        ('{"console_enabled":false}', "private Unsupported FILESYSTEM", False),
        ('{"console_enabled":false} private rustfsadmin', "", False),
        ("", '{"console_enabled":false}', True),
    ],
)
def test_probe_classifies_both_log_streams(
    stdout: str, stderr: str, passed: bool
) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs, failure="logs", result=Result(0, stdout, stderr))
    code, out, err = probe_run(probe_world(fs, runner, ProbeClock()))
    assert err == ""
    assert code == (0 if passed else 1)
    assert "private" not in out
    if not passed:
        rule = (
            "unsupported filesystem"
            if stderr
            else "default login"
            if "rustfsadmin" in stdout
            else "console line missing"
        )
        assert out == f"engine mode: local\nFAIL 1: {rule}\n"


def test_probe_init_gathers_with_the_process_uid() -> None:
    fs = validation_fixture()
    fs.denied_calls.add(("lstat", Path("/data")))
    with pytest.raises(PermissionError):
        rustfs_probe._initialize(world(fs, 0), Path("/data"))


@pytest.mark.parametrize("linux", [False, True])
def test_probe_platform_data_and_secret_owners(linux: bool) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs)
    system = probe_world(fs, runner, ProbeClock())
    fs.mkdir(Path("/temporary"))
    probe = rustfs_probe.Probe(system, {}, Path("/temporary"), linux=linux)
    probe.prepare(SOURCE)
    down = next((a, env) for a, env, _ in runner.calls if "down" in a)
    assert down[1]["O11Y_S3_ENV_FILE"] == "/temporary/data/secrets/s3.env"
    data_dir = "/temporary/data" if linux else "/var/lib/o11y-test/probe"
    assert fs.modes[Path("/temporary/release/.env")] & 0o777 == 0o600
    assert f"O11Y_DATA_DIR={data_dir}\n" in fs.read_text(
        Path("/temporary/release/.env")
    )
    assert probe.env == {"O11Y_S3_ENV_FILE": "/temporary/data/secrets/s3.env"}
    utility = [
        "docker",
        "run",
        "--rm",
        "--user",
        "0:0",
        "--network",
        "none",
        "-v",
        "/temporary:/d" if linux else "/var/lib/o11y-test:/d",
    ]
    destination = "/d/data" if linux else "/d/probe"
    expected = []
    if not linux:
        expected += [
            [*utility, "--entrypoint", "rm", "rustfs:pinned", "-rf", "/d/probe"],
            [
                *utility,
                "-v",
                "/temporary/data:/src:ro",
                "--entrypoint",
                "cp",
                "rustfs:pinned",
                "-a",
                "/src/.",
                "/d/probe/",
            ],
        ]
    expected.append(
        [
            *utility,
            "--entrypoint",
            "chown",
            "rustfs:pinned",
            "-R",
            "10001:10001",
            f"{destination}/rustfs",
        ]
    )
    for secret in init.SECRETS[:2]:
        expected.append(
            [
                *utility,
                "--entrypoint",
                "chown",
                "rustfs:pinned",
                f"{secret.uid}:{secret.gid}",
                f"{destination}/secrets/{secret.name}",
            ]
        )
        path = Path("/temporary/data/secrets") / secret.name
        assert fs.modes[path] & 0o777 == secret.mode == 0o400
        assert fs.owners[path] == (123, 456)
    assert [a for a, _, _ in runner.calls if a[:2] == ["docker", "run"]] == expected
    assert probe.cleanup()
    calls = [a for a, _, _ in runner.calls]
    assert calls[-1] == [
        *utility,
        "--entrypoint",
        "rm",
        "rustfs:pinned",
        "-rf",
        destination,
    ]
    assert all("sh" not in a and "-c" not in a for a in calls)
    assert Path("/temporary") not in fs.modes


def test_probe_secret_owners_come_from_init(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = tuple(replace(s, uid=s.uid + 1, gid=s.gid + 2) for s in init.SECRETS)
    monkeypatch.setattr(init, "SECRETS", secrets)
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs)
    fs.mkdir(Path("/temporary"))
    probe = rustfs_probe.Probe(
        probe_world(fs, runner, ProbeClock()), {}, Path("/temporary"), linux=True
    )
    probe.prepare(SOURCE)
    for secret in secrets[:2]:
        assert any(
            a[-4:]
            == [
                "chown",
                "rustfs:pinned",
                f"{secret.uid}:{secret.gid}",
                f"/d/data/secrets/{secret.name}",
            ]
            for a, _, _ in runner.calls
        )


@pytest.mark.parametrize(
    "setting",
    [
        'O11Y_DATA_DIR="/temporary/data"\n',
        "O11Y_DATA_DIR=/unresolved/data\n",
        "O11Y_DATA_DIR=/temporary/data\n" * 2,
    ],
)
def test_probe_data_redirect_requires_one_match(
    setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = validation_fixture()
    read_text = fs.read_text
    env_file = Path("/temporary/release/.env")

    def read(path: Path) -> str:
        return setting if path == env_file else read_text(path)

    monkeypatch.setattr(fs, "read_text", read)
    runner = ProbeRunner(fs=fs, identity="docker-desktop|Docker Desktop")
    system = replace(probe_world(fs, runner, ProbeClock()), platform="darwin")
    assert probe_run(system) == (
        1,
        "engine mode: local\nFAIL data dir could not be redirected\n",
        "",
    )
    assert not any("cp" in a or "chown" in a for a, _, _ in runner.calls)
    assert runner.calls[-2][0][-3:] == ["down", "-v", "--remove-orphans"]
    assert runner.calls[-1][0][-4:] == ["rm", "rustfs:pinned", "-rf", "/d/probe"]
    assert env_file not in fs.files
    assert Path("/temporary") not in fs.modes


def test_probe_requires_rustfs_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        init, "SECRETS", tuple(replace(s, guard="renamed/guard") for s in init.SECRETS)
    )
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs)
    system = probe_world(fs, runner, ProbeClock())
    assert probe_run(system) == (
        1,
        "engine mode: local\nFAIL rustfs secrets could not be found\n",
        "",
    )
    assert not any("up" in a for a, _, _ in runner.calls)
    assert runner.calls[-2][0][-3:] == ["down", "-v", "--remove-orphans"]
    assert runner.calls[-1][0][-4:] == ["rm", "rustfs:pinned", "-rf", "/d/data"]
    assert Path("/temporary") not in fs.modes


@pytest.mark.parametrize(
    ("log", "rule"),
    [
        ("private Unsupported filesystem", "unsupported filesystem"),
        ('{"console_enabled":false}', None),
        ("private", "console line missing"),
    ],
)
def test_probe_crash_loop_readiness_and_cleanup(
    log: str, rule: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(fs=fs, failure="9000/health/ready")
    original = runner.run

    def run_call(argv: Sequence[str], env: Mapping[str, str], timeout: int) -> Result:
        if "logs" in argv:
            runner.calls.append((list(argv), dict(env), timeout))
            return Result(0, log)
        return original(argv, env, timeout)

    monkeypatch.setattr(runner, "run", run_call)
    clock = ProbeClock()
    code, out, err = probe_run(probe_world(fs, runner, clock))
    suffix = f" ({rule})" if rule is not None else ""
    assert (code, out, err) == (
        1,
        f"engine mode: local\nFAIL 2: rustfs did not become ready{suffix}\n",
        "",
    )
    assert clock.sleeps == [2] * 60
    calls = [a for a, _, _ in runner.calls]
    log_index = next(i for i, a in enumerate(calls) if "logs" in a)
    assert all("logs" not in a for a in calls[:log_index])
    assert sum("9000/health/ready" in " ".join(a) for a in calls[:log_index]) == 61
    assert calls[-3] == ["docker", "rm", "-f", "o11y-probe-writer"]
    assert calls[-2][-3:] == ["down", "-v", "--remove-orphans"]
    assert calls[-1][-4:] == ["rm", "rustfs:pinned", "-rf", "/d/data"]
    assert Path("/temporary") not in fs.modes


@pytest.mark.parametrize(
    ("operation", "rule"),
    [
        ("--entrypoint rm", "leftover data could not be removed"),
        ("--entrypoint cp", "data copy failed"),
        ("secrets/rustfs.access_key", "init failed"),
        ("secrets/rustfs.secret_key", "init failed"),
    ],
)
def test_probe_vm_preparation_failures(operation: str, rule: str) -> None:
    fs = validation_fixture()
    runner = ProbeRunner(
        fs=fs,
        identity="docker-desktop|Docker Desktop",
        failure=operation,
    )
    system = replace(probe_world(fs, runner, ProbeClock()), platform="darwin")
    code, out, err = probe_run(system)
    assert (code, err) == (1, "")
    assert out.startswith(f"engine mode: local\nFAIL {rule}\n")
    assert Path("/temporary") not in fs.modes
    assert runner.calls[-1][0][-4:] == ["rm", "rustfs:pinned", "-rf", "/d/probe"]


DEDICATED = "unix:///run/dind/docker.sock"


def dedicated_fixture() -> tuple[Fake, ValidationRunner, cli.System]:
    fs = validation_fixture()
    fs.modes[Path("/run/dind/docker.sock")] = stat.S_IFSOCK | 0o600
    runner = ValidationRunner(
        {2: Result(0, DEDICATED + "\n"), 5: Result(0, "bridge\nhost\nnone\n")}
    )
    return fs, runner, replace(world(fs), runner=runner, platform="linux")


def test_dedicated_metadata_contract() -> None:
    _, runner, system = dedicated_fixture()
    env = {"PATH": "/bin"}
    assert validation.local_engine(system, env, dedicated=DEDICATED) is None
    before = {"PATH": "/bin", "DOCKER_HOST": DEDICATED}
    assert runner.calls == [
        (["docker", "context", "show"], before, 60),
        (
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                "default",
            ],
            before,
            60,
        ),
        (["docker", "ps", "-aq"], before, 60),
        (["docker", "volume", "ls", "-q"], before, 60),
        (["docker", "network", "ls", "--format", "{{.Name}}"], before, 60),
        (["docker", "version", "--format", "{{.Server.MinAPIVersion}}"], before, 60),
    ]
    assert env == before | {"DOCKER_API_VERSION": "1.44"}


@pytest.mark.parametrize(
    ("index", "result", "expected"),
    [
        (
            1,
            Result(0, "other"),
            "the dedicated engine requires the default Docker context",
        ),
        (
            2,
            Result(0, "unix:///other.sock"),
            "the Docker endpoint does not match the dedicated engine",
        ),
        (
            3,
            Result(0, "stopped-container\n"),
            "the dedicated engine already holds containers",
        ),
        (4, Result(0, "orphan-volume\n"), "the dedicated engine already holds volumes"),
        (
            5,
            Result(0, "bridge\ncustom\n"),
            "the dedicated engine already holds custom networks",
        ),
    ],
)
def test_dedicated_metadata_refusal(index: int, result: Result, expected: str) -> None:
    _, runner, system = dedicated_fixture()
    runner.replacements[index] = result
    assert validation.local_engine(system, {}, dedicated=DEDICATED) == expected
    assert len(runner.calls) == 5


@pytest.mark.parametrize("socket", [None, stat.S_IFREG | 0o600])
def test_dedicated_metadata_socket(socket: int | None) -> None:
    fs, _, system = dedicated_fixture()
    if socket is None:
        del fs.modes[Path("/run/dind/docker.sock")]
    else:
        fs.modes[Path("/run/dind/docker.sock")] = socket
    assert (
        validation.local_engine(system, {}, dedicated=DEDICATED)
        == "the dedicated engine endpoint is not a socket"
    )


@pytest.mark.parametrize("index", range(1, 7))
@pytest.mark.parametrize("code", [1, None])
def test_dedicated_metadata_failure(index: int, code: int | None) -> None:
    _, runner, system = dedicated_fixture()
    runner.replacements[index] = Result(code, "private", "private")
    messages = [
        "the Docker context could not be read",
        "the Docker context could not be inspected",
        "the dedicated engine containers could not be listed",
        "the dedicated engine volumes could not be listed",
        "the dedicated engine networks could not be listed",
        "the Docker API version could not be read",
    ]
    assert validation.local_engine(system, {}, dedicated=DEDICATED) == (
        "timed out" if code is None else messages[index - 1]
    )
    assert len(runner.calls) == index


@pytest.mark.parametrize("dedicated", [None, DEDICATED])
def test_dedicated_command_dispatch(dedicated: str | None) -> None:
    fs = validation_fixture()
    fs.modes[Path("/run/dind/docker.sock")] = stat.S_IFSOCK | 0o600
    runner = ProbeRunner(
        fs=fs,
        replacements={2: Result(0, DEDICATED), 5: Result(0, "bridge\nhost\nnone")},
    )
    args = ["o11y", "rustfs-probe", "--source", str(SOURCE)]
    if dedicated is not None:
        args.extend(["--dedicated-engine", dedicated])
    system = probe_world(fs, runner, ProbeClock())
    system = replace(
        system,
        env=dict(system.env)
        | {"O11Y_DEDICATED_ENGINE": DEDICATED, "DEDICATED_ENGINE": DEDICATED},
    )
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(args, system, out, err)
    mode = "local" if dedicated is None else f"dedicated {dedicated}"
    assert out.getvalue().startswith(f"engine mode: {mode}\n")
    assert out.getvalue().count("engine mode:") == 1
    assert err.getvalue() == ""
    if dedicated is None:
        assert code == 1
        assert "this machine's own daemon socket" in out.getvalue()
        assert all("DOCKER_HOST" not in env for _, env, _ in runner.calls)
    else:
        assert code == 0, out.getvalue()
        assert all(env["DOCKER_HOST"] == DEDICATED for _, env, _ in runner.calls)


def test_validate_refuses_dedicated_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(
            ["o11y", "validate", "--source", "/source", "--dedicated-engine", DEDICATED]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize("name", ["rustfs-probe", "smoke"])
def test_dedicated_engine_help(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["o11y", name, "--help"])
    assert exc.value.code == 0
    assert "--dedicated-engine ENDPOINT" in capsys.readouterr().out


@pytest.mark.parametrize("hostname", ["", "host-c-o11y"])
@pytest.mark.parametrize("fixed", ["", "false", "true"])
def test_render_opt_in_network(tmp_path: Path, hostname: str, fixed: str) -> None:
    source = o11y_source(tmp_path)
    output = tmp_path / "rendered"
    inputs = RENDER_ENV | {"O11Y_TS_HOSTNAME": hostname, "O11Y_FIXED_ADDRESSES": fixed}
    assert render_run(o11y.LocalFilesystem(), inputs, source=source, output=output) == (
        0,
        "",
        "",
    )
    text = (output / "release/compose.yaml").read_text()
    assert "@O11Y_" not in text
    document = yaml.safe_load(text)
    services = document["services"]
    assert "grafana-tls" not in services
    assert ("ts" in services) == bool(hostname)
    assert ("networks" in document) == (fixed == "true")
    for number, name in enumerate(
        ("ts", "loki", "tempo", "mimir", "alloy", "grafana", "grafana-owner"), 2
    ):
        if name not in services:
            continue
        service = services[name]
        assert ("extra_hosts" in service) == (fixed == "true")
        assert ("networks" in service) == (fixed == "true")
        if fixed == "true":
            assert service["networks"] == {
                "default": {"ipv4_address": f"203.0.113.{number}"}
            }
            assert service["extra_hosts"] == [
                f"{n}:203.0.113.{i}"
                for i, n in enumerate(
                    ("ts", "loki", "tempo", "mimir", "alloy", "grafana"), 2
                )
            ]
    if fixed == "true":
        assert document["networks"] == {
            "default": {
                "ipam": {
                    "config": [
                        {"subnet": "203.0.113.0/24", "ip_range": "203.0.113.128/25"}
                    ]
                }
            }
        }
    if hostname:
        sidecar = services["ts"]
        assert sidecar["environment"] == {
            "TS_AUTH_ONCE": "true",
            "TS_EXTRA_ARGS": "--advertise-tags=tag:o11y",
            "TS_HOSTNAME": "${O11Y_TS_HOSTNAME:?}",
            "TS_STATE_DIR": "/var/lib/tailscale",
            "TS_USERSPACE": "true",
            "TS_OUTBOUND_HTTP_PROXY_LISTEN": (
                {"true": "203.0.113.2:1055"}.get(fixed, "0.0.0.0:1055")
            ),
        }
        assert (
            sidecar["image"] == "tailscale/tailscale:v1.104.7@sha256:"
            "72c1e19e0aedca96b8533bbb0b4368564410ff20b17e53b804eb6c1f04a31be4"
        )
        assert sidecar["mem_limit"] == "256m"
        assert "ports" not in sidecar
        assert sidecar["volumes"] == [
            "${O11Y_DATA_DIR:?}/ts:/var/lib/tailscale",
            "${O11Y_CONFIG_TS:?}/resolv.conf:/etc/resolv.conf:ro",
        ]
        env = dict(
            line.split("=", 1)
            for line in (output / "release/.env").read_text().splitlines()
        )
        assert env["O11Y_TS_HOSTNAME"] == hostname
        assert (
            Path(env["O11Y_CONFIG_TS"]) / "resolv.conf"
        ).read_text() == "nameserver 198.51.100.41\nnameserver 198.51.100.106\n"
    baseline = yaml.safe_load(
        model.substitute(
            source.joinpath("compose.yaml").read_text(),
            model.Settings((), 50, email=False),
        )
    )
    services.pop("ts", None)
    for service in services.values():
        service.pop("networks", None)
        service.pop("extra_hosts", None)
    document.pop("networks", None)
    document.pop("x-hosts", None)
    assert document == baseline


def test_disabled_sidecar_keeps_later_config_directories() -> None:
    fs = rendered_fake()
    for name in ("ts", "z"):
        fs.mkdir(SOURCE / "config" / name)
        fs.write_atomic(SOURCE / "config" / name / "resolv.conf", b"resolver\n", 0o644)
    assert render_run(fs) == (0, "", "")
    assert "O11Y_CONFIG_TS" not in targets(fs)
    assert "O11Y_CONFIG_Z" in targets(fs)


def test_default_release_bytes(tmp_path: Path) -> None:
    source = o11y_source(tmp_path)
    output = tmp_path / "rendered"
    assert render_run(
        o11y.LocalFilesystem(),
        None,
        "--config-root",
        "/srv/render/config",
        source=source,
        output=output,
    ) == (0, "", "")
    files = {
        str(p.relative_to(output)): p.read_bytes()
        for p in output.rglob("*")
        if p.is_file()
    }
    # Snapshot of all rendered bytes and filenames.
    assert hashlib.sha256(model.manifest(files).encode()).hexdigest() == (
        "9c01e1c792c41f9d2c2bc8355f03007819d51f1e63e4afc16d8f3a36a8d566f7"
    )


@pytest.mark.parametrize("fixed", ["false", "true"])
@pytest.mark.parametrize("hostname", ["", "host-c-o11y"])
def test_render_tls_proxy(tmp_path: Path, fixed: str, hostname: str) -> None:
    source = o11y_source(tmp_path)
    output = tmp_path / "rendered"
    inputs = RENDER_ENV | {
        "O11Y_TS_HOSTNAME": hostname,
        "O11Y_FIXED_ADDRESSES": fixed,
        "O11Y_GRAFANA_TLS_CERT_DIR": "/srv/o11y/tls",
    }
    assert render_run(
        o11y.LocalFilesystem(),
        inputs,
        source=source,
        output=output,
    ) == (0, "", "")
    services = yaml.safe_load((output / "release/compose.yaml").read_text())["services"]
    assert ("ts" in services) == bool(hostname)
    proxy = services["grafana-tls"]
    expected = json.loads(
        (Path(__file__).parent / "testdata/render-tls-proxy.json").read_text()
    )
    assert proxy["depends_on"]["grafana"] == expected["grafana_dependency"]
    assert services["grafana"]["healthcheck"]["test"] == expected["probe"]
    for key, value in expected["proxy"].items():
        assert proxy[key] == value
    assert "ports" not in proxy
    assert proxy.get("networks", {}) == (
        {"default": {"ipv4_address": "203.0.113.9"}} if fixed == "true" else {}
    )
    environment = dict(
        line.split("=", 1)
        for line in (output / "release/.env").read_text().splitlines()
    )
    assert environment["O11Y_GRAFANA_TLS_CERT_DIR"] == "/srv/o11y/tls"
    config = (Path(environment["O11Y_CONFIG_GRAFANA_TLS"]) / "nginx.conf").read_text()
    assert "@O11Y_" not in config
    assert (
        "listen " + ("203.0.113.9:" if fixed == "true" else "") + "8443 ssl;" in config
    )
    assert (
        "proxy_pass http://"
        + ("203.0.113.7" if fixed == "true" else "grafana")
        + ":3000;"
        in config
    )
    for directive in expected["directives"]:
        assert directive in config
    assert model_validate.compose({"services": services}, {}) == []
