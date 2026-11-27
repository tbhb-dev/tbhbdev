"""Load the Connect token and endpoints for the current user.

On Linux, configuration management writes the token to a fixed path under
the user's home. On macOS, it uses the dedicated agent keychain.
"""

from __future__ import annotations

import os
import platform
import stat
import subprocess  # nosec B404
from pathlib import Path

from tbhbdev import host_executables, userconfig
from tbhbdev.host_tokens import LINUX_TOKEN_FILE
from tbhbdev.secrets.connect import SecretsError

# Where the token lives in the agent account. On macOS, security(1) resolves
# the keychain name inside ~/Library/Keychains; the service and account are
# the item's attributes. They name the tool, not a deployment: a host needs
# them before it reads any config.
KEYCHAIN = "tbhbdev.keychain"
SERVICE = "tbhbdev-connect"
ACCOUNT = "agent"
MAX_TOKEN_FILE = 1 << 16

# Tests only: the name of a variable holding a token to use instead of the
# keychain's. Nothing in production sets it.
TEST_TOKEN_ENV = "TBHBDEV_TEST_TOKEN"  # noqa: S105  # nosec B105
KEYCHAIN_TIMEOUT = 10.0


def _unsafe(found: os.stat_result) -> str | None:
    """What is wrong with the open token file FOUND, or None."""
    uid = os.geteuid()
    checks = (
        (not stat.S_ISREG(found.st_mode), "isn't a regular file"),
        (found.st_uid != uid, "isn't owned by this user"),
        (bool(found.st_mode & 0o077), "is accessible to its group or others"),
    )
    return next((problem for bad, problem in checks if bad), None)


def _linux_token() -> str:
    """The Connect token in the current user's 0600 file.

    The final directory and file components cannot be symlinks. Ancestor
    directories and later permission changes are outside these checks.
    """
    path = Path.home() / LINUX_TOKEN_FILE
    fix = "have configuration management install it"
    try:
        directory = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            found = os.fstat(directory)
            if found.st_uid != os.geteuid() or found.st_mode & 0o077:
                message = f"the Connect token directory {path.parent} is unsafe; {fix}"
                raise SecretsError(message)
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                problem = _unsafe(os.fstat(fd))
                if problem:
                    message = f"the Connect token file {path} {problem}; {fix}"
                    raise SecretsError(message)
                data = os.read(fd, MAX_TOKEN_FILE + 1)
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except OSError as exc:
        message = (
            f"the Connect token file {path} can't be opened: {exc.strerror}; {fix}"
        )
        raise SecretsError(message) from None
    if len(data) > MAX_TOKEN_FILE:
        message = f"the Connect token file {path} exceeds {MAX_TOKEN_FILE} bytes; {fix}"
        raise SecretsError(message)
    value = data.decode(errors="replace").strip()
    if not value:
        message = f"the Connect token file {path} is empty; {fix}"
        raise SecretsError(message)
    return value


def token() -> str:
    """The Connect token in the current user's keychain or token file.

    Raises:
        SecretsError: There is no usable token, or the keychain tool isn't
            installed.
    """
    override = os.environ.get(TEST_TOKEN_ENV)
    if override:
        return override
    if platform.system() != "Darwin":
        return _linux_token()
    try:
        security = host_executables.resolve("security")
        # The keychain has no password, so unlocking it is idempotent and
        # holds no secret. It is locked again after every restart.
        subprocess.run(  # nosec B603 B607
            [security, "unlock-keychain", "-p", "", KEYCHAIN],
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT,
            # False is the default, written out for ruff. The pragma hides the
            # equivalent check=None and dropped-check mutants, not check=True.
            check=(  # pragma: no mutate
                False
            ),
        )
        argv = [security, "find-generic-password", "-s", SERVICE, "-a", ACCOUNT]
        argv += ["-w", KEYCHAIN]
        done = subprocess.run(  # nosec B603
            argv,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT,
            # False is the default, written out for ruff. The pragma hides the
            # equivalent check=None and dropped-check mutants, not check=True.
            check=(  # pragma: no mutate
                False
            ),
        )
    except subprocess.TimeoutExpired:
        message = f"the keychain read timed out after {KEYCHAIN_TIMEOUT} seconds"
        raise SecretsError(message) from None
    except FileNotFoundError, host_executables.ExecutableLookupError:
        message = "can't read the Connect token: security isn't installed"
        raise SecretsError(message) from None
    value = done.stdout.rstrip("\n")
    if done.returncode != 0 or not value:
        message = (
            "no Connect token in this user's keychain. "
            "Use the operator's host tooling to install it."
        )
        raise SecretsError(message)
    return value


def endpoints() -> list[str]:
    """The user config's `[connect] endpoints`, in failover order.

    The operator's host tooling sets them on each agent host.

    Raises:
        SecretsError: The config doesn't list any.
    """
    try:
        return userconfig.strings("connect", "endpoints")
    except userconfig.ConfigError as exc:
        message = (
            f"{exc}. Use the operator's host tooling to set up this host's config."
        )
        raise SecretsError(message) from exc
