"""Descriptor operations refuse links and preserve the required fsync ordering."""

import errno
import os
import stat
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.protocol import RefusalError


def info(mode: int = stat.S_IFDIR | 0o755, uid: int = 0) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))


@pytest.fixture
def trusted_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Test descriptors on real files while substituting root credentials only."""
    original = os.fstat
    original_stat = os.stat

    def root_stat(fd: int) -> os.stat_result:
        actual = original(fd)
        fields = list(actual)
        fields[4] = 0
        # pytest's ancestors may be group writable. Product checks stay strict.
        fields[0] = actual.st_mode & ~0o022
        return os.stat_result(fields, {"st_mtime_ns": actual.st_mtime_ns})

    def root_path_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        actual = original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        fields = list(actual)
        fields[4] = 0
        return os.stat_result(fields, {"st_mtime_ns": actual.st_mtime_ns})

    monkeypatch.setattr(os, "stat", root_path_stat)
    monkeypatch.setattr(os, "fstat", root_stat)

    def ignore_owner(*_args: object) -> None:
        pass

    monkeypatch.setattr(os, "fchown", ignore_owner)
    return Path(os.path.realpath(tmp_path))


@pytest.mark.parametrize(
    ("mode", "uid"),
    [
        (stat.S_IFREG | 0o644, 0),
        (stat.S_IFDIR | 0o775, 0),
        (stat.S_IFDIR | 0o757, 0),
        (stat.S_IFDIR | 0o755, 1),
    ],
)
def test_untrusted_directory(mode: int, uid: int) -> None:
    with pytest.raises(RefusalError):
        fs.directory(info(mode, uid))


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "/srv/../etc",
        "/srv/./data",
        "/srv/$HOME",
        "/srv/~user",
        "/srv/space name",
    ],
)
def test_spelling(path: str) -> None:
    with pytest.raises(RefusalError):
        fs.spelling(path)


def test_real_descriptor_roundtrip(trusted_tree: Path) -> None:
    data = trusted_tree / "data"
    fs.mkdir(data)
    fs.mkdir(data)
    target = data / "token"
    assert fs.inspect(target, file=True, missing=True) is None
    fs.write(target, b"first", 0o400)
    assert fs.read(target) == b"first"
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    fs.write(target, b"second")
    assert fs.read(target) == b"second"
    assert fs.inspect(target, file=True) is not None
    assert fs.inspect(data) is not None
    fs.sync(data)
    fs.unlink(target)
    fs.unlink(target)
    with pytest.raises(RefusalError, match=r"^missing path$"):
        fs.inspect(target)
    assert list(data.iterdir()) == []


def test_links_and_non_files(trusted_tree: Path) -> None:
    target = trusted_tree / "target"
    target.write_bytes(b"unchanged")
    link = trusted_tree / "link"
    link.symlink_to(target)
    with pytest.raises(RefusalError, match=r"^symlink path$"):
        fs.inspect(link)
    with pytest.raises(RefusalError, match=r"^secret target type$"):
        fs.write(link, b"new")
    with pytest.raises(OSError, match=r"."):
        fs.read(link)
    with pytest.raises(RefusalError):
        fs.mkdir(link)
    assert target.read_bytes() == b"unchanged"
    directory = trusted_tree / "dir"
    directory.mkdir()
    with pytest.raises(RefusalError, match=r"^path type$"):
        fs.inspect(directory, file=True)
    with pytest.raises(RefusalError, match=r"^file type$"):
        fs.read(directory)
    child = directory / "file"
    child.write_bytes(b"x")
    link.unlink()
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError, match=r"."):
        fs.read(link / "file")
    target.chmod(0o666)
    with pytest.raises(RefusalError, match=r"^writable control file$"):
        fs.inspect(target, file=True)


def test_pointer_unset_and_replace(trusted_tree: Path) -> None:
    path = trusted_tree / "current"
    assert fs.target(path) is None
    fs.pointer(path, "releases/" + "a" * 40)
    assert fs.target(path) == "releases/" + "a" * 40
    fs.pointer(path, "releases/" + "b" * 40)
    assert fs.target(path) == "releases/" + "b" * 40
    fs.pointer(path, None)
    fs.pointer(path, None)
    assert fs.target(path) is None
    path.symlink_to("/outside")
    with pytest.raises(RefusalError, match=r"^pointer target$"):
        fs.target(path)


def test_write_failure_cleans_temporary(
    trusted_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*_args: object, **_kwargs: object) -> None:
        message = "injected rename failure"
        raise OSError(message)

    monkeypatch.setattr(os, "rename", failed)
    with pytest.raises(OSError, match="injected rename failure"):
        fs.write(trusted_tree / "token", b"fixture")
    assert list(trusted_tree.iterdir()) == []
    with pytest.raises(OSError, match="injected rename failure"):
        fs.pointer(trusted_tree / "current", "releases/" + "a" * 40)
    assert list(trusted_tree.iterdir()) == []


def test_platform_and_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RefusalError), fs.parent(Path("relative")):
        pytest.fail("opened relative path")
    monkeypatch.setattr(fs.sys, "platform", "win32")
    with pytest.raises(OSError, match=r"^receive requires Unix$"):
        fs.open_entry("/")


def test_write_without_unix_owner_call(
    trusted_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fs.open_entry

    def open_unix(
        name: str,
        flags: int = os.O_RDONLY,
        *,
        directory: bool = False,
        dir_fd: int | None = None,
    ) -> int:
        with monkeypatch.context() as patch:
            patch.setattr(fs.sys, "platform", "darwin")
            return original(name, flags, directory=directory, dir_fd=dir_fd)

    monkeypatch.setattr(fs, "open_entry", open_unix)
    monkeypatch.setattr(fs.sys, "platform", "win32")
    with patch.object(os, "fchown", create=True) as owner:
        fs.write(trusted_tree / "file", b"fixture")
    owner.assert_not_called()
    assert (trusted_tree / "file").read_bytes() == b"fixture"


@pytest.mark.parametrize(
    ("mode", "uid", "reason"),
    [
        (stat.S_IFREG | 0o700, 0, "path component is not a directory"),
        (stat.S_IFDIR | 0o700, 1, "directory owner"),
        (stat.S_IFDIR | 0o720, 0, "writable directory"),
        (stat.S_IFDIR | 0o702, 0, "writable directory"),
    ],
)
def test_directory_exact_refusals(mode: int, uid: int, reason: str) -> None:
    with pytest.raises(RefusalError) as caught:
        fs.directory(info(mode, uid))
    assert str(caught.value) == reason


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755, 0o705])
def test_directory_trusted_modes(mode: int) -> None:
    fs.directory(info(stat.S_IFDIR | mode))


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("relative", "path characters"),
        ("/a b", "path characters"),
        ("/a/./b", "path components"),
        ("/a/../b", "path components"),
    ],
)
def test_spelling_exact_errors(path: str, reason: str) -> None:
    with pytest.raises(RefusalError) as caught:
        fs.spelling(path)
    assert str(caught.value) == reason
    assert fs.spelling("/srv/name_1.2-x/data") == Path("/srv/name_1.2-x/data")
    assert fs.spelling("/srv/Name_1.2-X/data") == Path("/srv/Name_1.2-X/data")


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_WRONLY | os.O_CREAT | os.O_EXCL])
def test_open_entry_exact_flags(directory: bool, flags: int) -> None:
    if sys.platform == "win32":
        pytest.skip("Unix descriptors required")
    with patch.object(os, "open", return_value=73) as opened:
        assert fs.open_entry("entry", flags, directory=directory, dir_fd=41) == 73
    opened.assert_called_once_with(
        "entry",
        flags
        | getattr(os, "O_NOFOLLOW", 0)
        | (getattr(os, "O_DIRECTORY", 0) if directory else 0),
        0o600,
        dir_fd=41,
    )


def test_write_owner_mode_and_durability(trusted_tree: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Unix ownership required")
    path = trusted_tree / "token"
    with (
        patch.object(os, "fchown", wraps=getattr(os, "fchown", None)) as owner,
        patch.object(os, "fchmod", wraps=os.fchmod) as mode,
        patch.object(os, "fsync", wraps=os.fsync) as sync,
        patch.object(os, "rename", wraps=os.rename) as rename,
        patch.object(os, "unlink", wraps=os.unlink) as unlink,
    ):
        manager = Mock()
        for name, method in (
            ("owner", owner),
            ("mode", mode),
            ("sync", sync),
            ("rename", rename),
            ("unlink", unlink),
        ):
            manager.attach_mock(method, name)
        fs.write(path, b"payload", 0o640, 123, 456)
    target_fd = owner.call_args.args[0]
    parent_fd = rename.call_args.kwargs["src_dir_fd"]
    temporary = rename.call_args.args[0]
    assert isinstance(temporary, str)
    assert temporary.startswith(".tmp-")
    assert len(temporary) == 37
    assert manager.mock_calls == [
        call.owner(target_fd, 123, 456),
        call.mode(target_fd, 0o640),
        call.sync(target_fd),
        call.rename(temporary, "token", src_dir_fd=parent_fd, dst_dir_fd=parent_fd),
        call.sync(parent_fd),
        call.unlink(temporary, dir_fd=parent_fd),
    ]
    assert path.read_bytes() == b"payload"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert sorted(entry.name for entry in trusted_tree.iterdir()) == ["token"]


def test_inspect_uses_parent_descriptor_without_following_links() -> None:
    record = info(stat.S_IFREG | 0o755)
    with (
        patch.object(fs, "parent") as parent,
        patch.object(os, "stat", return_value=record) as metadata,
    ):
        parent.return_value.__enter__.return_value = (73, "token")
        assert fs.inspect(Path("/srv/token"), file=True) is record
    metadata.assert_called_once_with("token", dir_fd=73, follow_symlinks=False)


def test_read_keeps_descriptor_ownership(trusted_tree: Path) -> None:
    path = trusted_tree / "token"
    path.write_bytes(b"complete\x00payload")
    with patch.object(os, "fdopen", wraps=os.fdopen) as opened:
        assert fs.read(path) == b"complete\x00payload"
    opened.assert_called_once_with(opened.call_args.args[0], "rb", closefd=False)


def test_write_defaults_to_root_private_file(trusted_tree: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Unix ownership required")
    path = trusted_tree / "private"
    with patch.object(os, "fchown", wraps=getattr(os, "fchown", None)) as owner:
        fs.write(path, b"private")
    owner.assert_called_once_with(owner.call_args.args[0], 0, 0)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == b"private"


@pytest.mark.parametrize("mode", [None, 0o750])
def test_mkdir_passes_mode_and_descriptor(trusted_tree: Path, mode: int | None) -> None:
    path = trusted_tree / "private"
    with (
        patch.object(os, "mkdir", wraps=os.mkdir) as mkdir,
        patch.object(os, "chmod", wraps=os.chmod) as chmod,
    ):
        if mode is None:
            fs.mkdir(path)
        else:
            fs.mkdir(path, mode)
    expected = 0o700 if mode is None else mode
    mkdir.assert_called_once_with(
        "private", mode=expected, dir_fd=mkdir.call_args.kwargs["dir_fd"]
    )
    chmod.assert_called_once_with(
        "private",
        expected,
        dir_fd=mkdir.call_args.kwargs["dir_fd"],
        follow_symlinks=False,
    )
    assert stat.S_IMODE(path.stat().st_mode) == expected


@pytest.mark.parametrize("operation", ["mkdir", "write"])
def test_existing_destination_is_checked_in_its_parent(
    trusted_tree: Path, operation: str
) -> None:
    path = trusted_tree / "existing"
    if operation == "mkdir":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"old")
    with patch.object(os, "stat", wraps=os.stat) as metadata:
        if operation == "mkdir":
            fs.mkdir(path)
        else:
            fs.write(path, b"new")
    parent_fd = metadata.call_args.kwargs["dir_fd"]
    assert [item for item in metadata.call_args_list if "dir_fd" in item.kwargs] == [
        call("existing", dir_fd=parent_fd, follow_symlinks=False)
    ]
    if operation == "write":
        assert path.read_bytes() == b"new"


def test_sync_opens_a_directory_descriptor(trusted_tree: Path) -> None:
    with patch.object(fs, "open_entry", wraps=fs.open_entry) as opened:
        fs.sync(trusted_tree)
    assert opened.call_args == call(
        trusted_tree.name, directory=True, dir_fd=opened.call_args.kwargs["dir_fd"]
    )


@pytest.mark.parametrize("mode", [0o620, 0o602])
def test_control_read_validates_opened_file_mode(
    trusted_tree: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    path = trusted_tree / "control"
    path.write_bytes(b"private")
    monkeypatch.setattr(fs, "inspect", Mock())
    original = os.fstat

    def writable(fd: int) -> os.stat_result:
        actual = original(fd)
        if stat.S_ISREG(actual.st_mode):
            return info(stat.S_IFREG | mode)
        return actual

    monkeypatch.setattr(os, "fstat", writable)
    with pytest.raises(RefusalError, match=r"^writable control file$"):
        fs.control_read(path)


def test_control_read_preserves_other_os_errors(trusted_tree: Path) -> None:
    failure = OSError(errno.EACCES, "denied")
    with (
        patch.object(fs, "inspect", side_effect=failure),
        pytest.raises(OSError, match="denied") as caught,
    ):
        fs.control_read(trusted_tree / "control")
    assert caught.value is failure


def test_control_read_refuses_a_final_symlink_swap(trusted_tree: Path) -> None:
    target = trusted_tree / "target"
    target.write_bytes(b"data")
    link = trusted_tree / "link"
    link.symlink_to(target)
    with (
        patch.object(fs, "inspect"),
        pytest.raises(RefusalError, match=r"^control path symlink or non-directory$"),
    ):
        fs.control_read(link)


def test_control_read_allows_executable_mode(trusted_tree: Path) -> None:
    path = trusted_tree / "binary"
    path.write_bytes(b"program")
    path.chmod(0o555)
    assert fs.control_read(path) == b"program"


def test_ordinary_secret_reads_keep_their_own_owner_and_mode(
    trusted_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = trusted_tree / "secret"
    path.write_bytes(b"data")
    original = os.fstat

    def secret_info(fd: int) -> os.stat_result:
        actual = original(fd)
        return info(stat.S_IFREG | 0o666, 1) if stat.S_ISREG(actual.st_mode) else actual

    monkeypatch.setattr(os, "fstat", secret_info)
    assert fs.read(path) == b"data"
    with pytest.raises(RefusalError, match=r"^control file owner$"):
        fs.read(path, control=True)


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o700])
def test_mkdir_sets_exact_mode_under_private_umask(
    trusted_tree: Path, mode: int
) -> None:
    previous = os.umask(0o077)
    try:
        path = trusted_tree / "directory"
        fs.mkdir(path, mode)
        assert stat.S_IMODE(path.stat().st_mode) == mode
    finally:
        os.umask(previous)


@pytest.mark.parametrize("mode", [0o622, 0o646])
def test_real_control_file_write_bits(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "configuration"
    target.write_bytes(b"configuration")
    target.chmod(mode)
    fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        # Isolate the leaf check without substituting stat, fstat or mode bits.
        with (
            patch.object(fs, "parent", return_value=nullcontext((fd, target.name))),
            pytest.raises(RefusalError, match=r"^writable control file$"),
        ):
            fs.inspect(target, file=True)
    finally:
        os.close(fd)


def test_real_control_file_symlink(tmp_path: Path) -> None:
    target = tmp_path / "configuration"
    target.write_bytes(b"configuration")
    link = tmp_path / "link"
    link.symlink_to(target)
    fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with (
            patch.object(fs, "parent", return_value=nullcontext((fd, link.name))),
            pytest.raises(RefusalError, match=r"^symlink path$"),
        ):
            fs.control_read(link)
    finally:
        os.close(fd)


@pytest.mark.parametrize("mode", [0o775, 0o757])
def test_real_directory_write_bits(tmp_path: Path, mode: int) -> None:
    directory = tmp_path / "configuration"
    directory.mkdir(mode=0o700)
    directory.chmod(mode)
    fields = list(directory.stat())
    # Only ownership is synthetic. Retain the actual mode from the real file.
    fields[4] = 0
    with pytest.raises(RefusalError, match=r"^writable directory$"):
        fs.directory(os.stat_result(fields))


def test_real_nonroot_directory_owner(tmp_path: Path) -> None:
    actual = tmp_path.stat()
    assert actual.st_uid != 0
    with pytest.raises(RefusalError, match=r"^directory owner$"):
        fs.directory(actual)
