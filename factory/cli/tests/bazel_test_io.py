# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Block I/O and isolate home directories in guarded CLI test targets."""

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Never


def _blocked_call(name: str) -> Callable[..., Never]:
    def blocked(*_args: object, **_kwargs: object) -> Never:
        msg = f"CLI unit tests must not perform I/O: {name} is blocked"
        raise RuntimeError(msg)

    return blocked


def block_io(patch: pytest.MonkeyPatch) -> None:
    patch.setattr(socket.socket, "connect", _blocked_call("socket.socket.connect"))
    patch.setattr(
        subprocess.Popen, "__init__", _blocked_call("subprocess.Popen.__init__")
    )
    patch.setattr(time, "sleep", _blocked_call("time.sleep"))
    for name in ("fork", "system", *(n for n in dir(os) if n.startswith("exec"))):
        if hasattr(os, name):
            patch.setattr(os, name, _blocked_call(f"os.{name}"))


def redirect_home(patch: pytest.MonkeyPatch, home: Path) -> None:
    patch.setenv("HOME", str(home))
    for variable in (
        "XDG_CACHE_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_DIRS",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    ):
        patch.setenv(variable, str(home / variable.lower()))


@pytest.hookimpl(wrapper=True, tryfirst=True)  # noqa: V103
def pytest_load_initial_conftests(early_config: pytest.Config) -> Iterator[None]:
    """Cover imports, collection, and fixtures for the entire pytest run."""
    home = tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"])
    patch = pytest.MonkeyPatch()
    early_config.add_cleanup(home.cleanup)
    early_config.add_cleanup(patch.undo)
    with pytest.MonkeyPatch.context() as conftest_env:
        conftest_env.setenv("CLI_TEST_ACCOUNT_HOME", str(Path.home()))
        redirect_home(patch, Path(home.name))
        block_io(patch)
        yield


@pytest.fixture(autouse=True)
def isolate_test_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give each test and its function fixtures their own home."""
    redirect_home(monkeypatch, tmp_path)
