"""Check user configuration and the token file and keychain readers."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import traceback
from typing import TYPE_CHECKING
lazy from pathlib import Path

import pytest
from stub_fixtures import stubs as stubs  # noqa: PLC0414

from tbhbdev import host_executables
from tbhbdev.host_tokens import LINUX_TOKEN_FILE
from tbhbdev.secrets import config as secret_config
from tbhbdev.secrets.connect import SecretsError

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import Installed
    from stub_fixtures import Stubs


def test_endpoints_come_from_the_user_config(
    config: Callable[[list[str]], None],
) -> None:
    config(["https://connect-a.example.ts.net", "https://connect-b.example.ts.net"])
    assert secret_config.endpoints() == [
        "https://connect-a.example.ts.net",
        "https://connect-b.example.ts.net",
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (None, "doesn't exist; create it and set [connect] endpoints"),
        ("[connect\n", "cannot parse "),
        ("", "sets no [connect] endpoints"),
        ("connect = 1\n", "sets no [connect] endpoints"),
        ("[connect]\nendpoints = []\n", "[connect] endpoints must be a non-empty"),
        ('[connect]\nendpoints = "https://a"\n', "endpoints must be a non-empty"),
    ],
)
def test_endpoints_rejects_a_bad_config(
    config_file: Path, text: str | None, error: str
) -> None:
    if text is not None:
        config_file.write_text(text)
    with pytest.raises(SecretsError) as caught:
        secret_config.endpoints()
    assert str(config_file) in str(caught.value)
    assert error in str(caught.value)
    assert str(caught.value).endswith(
        ". Use the operator's host tooling to set up this host's config."
    )


def test_a_plugin_config_is_not_read(installed: Installed) -> None:
    installed.stub(
        "stub", files={"secret_config.toml": '[connect]\nendpoints = ["x"]\n'}
    )
    with pytest.raises(SecretsError, match="doesn't exist"):
        secret_config.endpoints()


# The token in the keychain.


def test_token_override_skips_the_keychain(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secret_config.TEST_TOKEN_ENV, "fake-override")
    assert secret_config.token() == "fake-override"
    assert stubs.calls() == []


def mac_keychain(stubs: Stubs, home: Path, value: str | None) -> None:
    """A macOS agent keychain under HOME, holding VALUE when it isn't None."""
    path = home / "Library" / "Keychains" / f"{secret_config.KEYCHAIN}-db"
    path.parent.mkdir(parents=True)
    items: dict[str, str] = {}
    if value is not None:
        items[f"{secret_config.SERVICE}/{secret_config.ACCOUNT}"] = value
    path.write_text(json.dumps({"password": "", "locked": True, "items": items}))
    assert stubs.calls() == []


def test_token_on_macos_unlocks_then_reads_the_agent_keychain(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Darwin")
    mac_keychain(stubs, tmp_path / "home", "fake-mac-token")
    assert secret_config.token() == "fake-mac-token"
    assert stubs.argvs("security") == [
        ["unlock-keychain", "-p", "", secret_config.KEYCHAIN],
        [
            "find-generic-password",
            "-s",
            secret_config.SERVICE,
            "-a",
            secret_config.ACCOUNT,
            "-w",
            secret_config.KEYCHAIN,
        ],
    ]


@pytest.mark.parametrize("make", [True, False])
def test_token_on_macos_without_an_entry_fails(
    stubs: Stubs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    make: bool,
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Darwin")
    if make:
        mac_keychain(stubs, tmp_path / "home", None)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == (
        "no Connect token in this user's keychain. "
        "Use the operator's host tooling to install it."
    )
    # Not even security's own complaint about a missing keychain.
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("value", ["fake-token ", "fake-tokenX"])
def test_token_strips_only_the_newline_security_adds(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Darwin")
    mac_keychain(stubs, tmp_path / "home", value)
    assert secret_config.token() == value


HINT = "have configuration management install it"


@pytest.fixture
def linux_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home for a Linux user, with a private secrets directory."""
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Linux")
    monkeypatch.delenv(secret_config.TEST_TOKEN_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / ".config" / "tbhbdev" / "secrets").mkdir(parents=True)
    (tmp_path / ".config" / "tbhbdev" / "secrets").chmod(0o700)
    return tmp_path


def token_file(home: Path, text: str | None = "fake-linux-token\n") -> Path:
    path = home / ".config" / "tbhbdev" / "secrets" / "connect-token"
    if text is not None:
        path.write_text(text)
        path.chmod(0o600)
    return path


def test_the_linux_token_path_is_under_home() -> None:
    assert LINUX_TOKEN_FILE.as_posix() == ".config/tbhbdev/secrets/connect-token"


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("fake-linux-token\n", "fake-linux-token"),
        ("fake-linux-token", "fake-linux-token"),
        ("fake-linux-token\n\n", "fake-linux-token"),
        ("fake-linux-token ", "fake-linux-token"),
        (" \t\r\nfake-linux-token \r\n", "fake-linux-token"),
    ],
)
def test_token_on_linux_reads_the_file(linux_home: Path, text: str, value: str) -> None:
    token_file(linux_home, text)
    assert secret_config.token() == value


def test_token_on_linux_ignores_xdg_config_home(linux_home: Path) -> None:
    token_file(linux_home)
    other = linux_home / "xdg" / "tbhbdev" / "secrets"
    other.mkdir(parents=True)
    (other / "connect-token").write_text("wrong")
    assert secret_config.token() == "fake-linux-token"


def refused(path: Path, problem: str) -> str:
    return f"the Connect token file {path} {problem}; {HINT}"


def test_token_on_linux_without_a_file_fails(linux_home: Path) -> None:
    path = token_file(linux_home, None)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == refused(
        path, "can't be opened: No such file or directory"
    )


def test_token_on_linux_without_a_directory_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Linux")
    monkeypatch.delenv(secret_config.TEST_TOKEN_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SecretsError, match="can't be opened"):
        secret_config.token()


@pytest.mark.parametrize("text", ["", "\n", " \t\r\n"])
def test_token_on_linux_with_an_empty_file_fails(linux_home: Path, text: str) -> None:
    path = token_file(linux_home, text)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == refused(path, "is empty")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o602, 0o610, 0o601])
def test_token_on_linux_refuses_a_file_open_to_others(
    linux_home: Path, mode: int
) -> None:
    path = token_file(linux_home)
    path.chmod(mode)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == refused(path, "is accessible to its group or others")
    assert "fake-linux-token" not in str(caught.value)


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770, 0o701, 0o710, 0o702])
def test_token_on_linux_refuses_a_directory_open_to_others(
    linux_home: Path, mode: int
) -> None:
    path = token_file(linux_home)
    path.parent.chmod(mode)
    try:
        with pytest.raises(SecretsError) as caught:
            secret_config.token()
    finally:
        path.parent.chmod(0o700)
    assert str(caught.value) == (
        f"the Connect token directory {path.parent} is unsafe; {HINT}"
    )


def test_token_on_linux_refuses_a_symlink(linux_home: Path) -> None:
    path = token_file(linux_home, None)
    real = linux_home / "real-token"
    real.write_text("fake-linux-token\n")
    real.chmod(0o600)
    path.symlink_to(real)
    with pytest.raises(SecretsError, match="can't be opened") as caught:
        secret_config.token()
    assert "fake-linux-token" not in str(caught.value)


def test_token_on_linux_refuses_a_symlinked_directory(linux_home: Path) -> None:
    path = token_file(linux_home)
    moved = linux_home / "moved"
    path.parent.rename(moved)
    path.parent.symlink_to(moved)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert "can't be opened" in str(caught.value)


def test_token_on_linux_refuses_a_file_another_user_owns(
    linux_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = token_file(linux_home)
    real_stat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        found = real_stat(fd)
        values = list(found)
        if stat.S_ISREG(found.st_mode):
            values[4] += 1
        return os.stat_result(values)

    monkeypatch.setattr(secret_config.os, "fstat", fstat)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == refused(path, "isn't owned by this user")


def test_token_on_linux_refuses_a_directory_another_user_owns(
    linux_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = token_file(linux_home)
    me = os.geteuid()
    real_stat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        found = real_stat(fd)
        if stat.S_ISDIR(found.st_mode):
            values = list(found)
            values[4] = me + 1
            return os.stat_result(values)
        return found

    monkeypatch.setattr(secret_config.os, "fstat", fstat)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == (
        f"the Connect token directory {path.parent} is unsafe; {HINT}"
    )


def test_token_on_linux_refuses_a_fifo(linux_home: Path) -> None:
    path = token_file(linux_home, None)
    os.mkfifo(path, 0o600)
    with pytest.raises(SecretsError, match="isn't a regular file"):
        secret_config.token()


def test_token_without_the_keychain_tool_fails(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Darwin")
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(host_executables, "search", lambda: str(tmp_path / "empty"))
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == (
        "can't read the Connect token: security isn't installed"
    )
    assert stubs.calls() == []


@pytest.fixture
def resolved_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    executable = str(tmp_path / "host-bin" / "security")

    def resolve(name: str) -> str:
        assert name == "security"
        return executable

    monkeypatch.setattr(host_executables, "resolve", resolve)
    return executable


@pytest.mark.parametrize("command", ["unlock-keychain", "find-generic-password"])
def test_keychain_timeout(
    monkeypatch: pytest.MonkeyPatch, command: str, resolved_security: str
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Darwin")
    monkeypatch.delenv(secret_config.TEST_TOKEN_ENV, raising=False)
    commands: list[str] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == resolved_security
        assert kwargs["timeout"] == secret_config.KEYCHAIN_TIMEOUT == 10.0
        commands.append(argv[1])
        if argv[1] == command:
            raise subprocess.TimeoutExpired(
                argv,
                secret_config.KEYCHAIN_TIMEOUT,
                output="stdout-marker",
                stderr="stderr-marker",
            )
        return subprocess.CompletedProcess(argv, 0, "fake-token\n", "")

    monkeypatch.setattr(secret_config.subprocess, "run", run)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == "the keychain read timed out after 10.0 seconds"
    assert commands[-1] == command
    message = "".join(traceback.format_exception(caught.value))
    assert "stdout-marker" not in message
    assert "stderr-marker" not in message


def fake_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every command token runs, each printing a fake token and succeeding."""
    commands: list[list[str]] = []

    def run(argv: list[str], **_: bool) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "fake-token\n", "")

    monkeypatch.setattr(secret_config.subprocess, "run", run)
    monkeypatch.delenv(secret_config.TEST_TOKEN_ENV, raising=False)
    return commands


# macOS finds a stub tool whatever the case of its name, so these check the
# names token runs without the stubs.


def test_token_on_macos_uses_resolved_security(
    monkeypatch: pytest.MonkeyPatch, resolved_security: str
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Darwin")
    commands = fake_runs(monkeypatch)
    assert secret_config.token() == "fake-token"
    assert [argv[:2] for argv in commands] == [
        [resolved_security, "unlock-keychain"],
        [resolved_security, "find-generic-password"],
    ]


@pytest.mark.parametrize(("returncode", "stdout"), [(0, ""), (1, "fake-token\n")])
def test_macos_rejects_empty_or_failed_keychain_reads(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    resolved_security: str,
) -> None:
    monkeypatch.setattr(secret_config.platform, "system", lambda: "Darwin")

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == resolved_security
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(secret_config.subprocess, "run", run)
    with pytest.raises(SecretsError) as caught:
        secret_config.token()
    assert str(caught.value) == (
        "no Connect token in this user's keychain. "
        "Use the operator's host tooling to install it."
    )


def test_linux_replaces_invalid_utf8(linux_home: Path) -> None:
    path = token_file(linux_home)
    path.write_bytes(b"fake-\xff-token\n")
    assert secret_config.token() == "fake-\ufffd-token"


@pytest.mark.parametrize("fail", ["directory", "file", "read", "none"])
def test_linux_token_descriptors_are_checked_and_closed(
    linux_home: Path, monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    path = token_file(linux_home)
    real_open, real_fstat, real_read = os.open, os.fstat, os.read
    opened: list[int] = []
    failed = False

    def open_checked(name: str | Path, flags: int, **kwargs: int) -> int:
        if not opened:
            assert name == path.parent
            assert flags == os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            assert kwargs == {}
        else:
            assert name == "connect-token"
            assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            assert kwargs == {"dir_fd": opened[0]}
            moved = linux_home / "moved"
            path.parent.rename(moved)
            path.parent.mkdir()
            token_file(linux_home, "replacement")
        fd = real_open(name, flags, **kwargs)
        opened.append(fd)
        return fd

    def fstat_checked(fd: int) -> os.stat_result:
        nonlocal failed
        if (
            fd in opened
            and not failed
            and fail == ("directory" if fd == opened[0] else "file")
        ):
            failed = True
            message = "stat failed"
            raise OSError(message)
        return real_fstat(fd)

    def read_checked(fd: int, count: int) -> bytes:
        assert count == secret_config.MAX_TOKEN_FILE + 1
        if fail == "read":
            message = "read failed"
            raise OSError(message)
        return real_read(fd, count)

    monkeypatch.setattr(secret_config.os, "open", open_checked)
    monkeypatch.setattr(secret_config.os, "fstat", fstat_checked)
    monkeypatch.setattr(secret_config.os, "read", read_checked)
    if fail == "none":
        assert secret_config.token() == "fake-linux-token"
    else:
        with pytest.raises(SecretsError, match="can't be opened"):
            secret_config.token()
    for fd in opened:
        with pytest.raises(OSError, match="Bad file descriptor"):
            real_fstat(fd)


@pytest.mark.parametrize(
    "size",
    [
        secret_config.MAX_TOKEN_FILE - 1,
        secret_config.MAX_TOKEN_FILE,
        secret_config.MAX_TOKEN_FILE + 1,
    ],
)
def test_linux_token_size_limit(linux_home: Path, size: int) -> None:
    path = token_file(linux_home, "x" * size)
    if size > secret_config.MAX_TOKEN_FILE:
        with pytest.raises(SecretsError) as caught:
            secret_config.token()
        assert str(caught.value) == refused(
            path, f"exceeds {secret_config.MAX_TOKEN_FILE} bytes"
        )
    else:
        assert secret_config.token() == "x" * size
