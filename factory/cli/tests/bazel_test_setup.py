# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Set up pytest's working directory and pinned tools in Bazel runfiles."""

import os
import socket
import subprocess
import sys
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


@pytest.fixture(autouse=True)
def guard_unit_test_io(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Block I/O calls and give each CLI unit test a temporary home."""
    if os.environ.get("CLI_TEST_KIND") == "io":
        # Keep this runner setting out of the environment the CLI sees.
        monkeypatch.delenv("CLI_TEST_KIND")
        return

    tmp_path: Path = request.getfixturevalue("tmp_path")
    monkeypatch.setenv("HOME", str(tmp_path))
    for variable in (
        "XDG_CACHE_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_DIRS",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

    monkeypatch.setattr(
        socket.socket, "connect", _blocked_call("socket.socket.connect")
    )
    monkeypatch.setattr(
        subprocess.Popen, "__init__", _blocked_call("subprocess.Popen.__init__")
    )
    monkeypatch.setattr(time, "sleep", _blocked_call("time.sleep"))
    for name in ("fork", "system", *(n for n in dir(os) if n.startswith("exec"))):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _blocked_call(f"os.{name}"))


@pytest.hookimpl(wrapper=True, tryfirst=True)  # noqa: V103
def pytest_load_initial_conftests(args: list[str]) -> Iterator[None]:
    """Set up the environment before pytest imports the shared fixtures."""
    workspace = Path.cwd()
    tools = os.environ["CLI_TEST_TOOLS"].split()
    path = [str(workspace / Path(tool).parent) for tool in tools]
    os.environ["PATH"] = os.pathsep.join([*path, os.environ.get("PATH", os.defpath)])
    # Some tests start a fresh Python interpreter and forward PYTHONPATH.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        p for p in sys.path if Path(p).is_absolute()
    )
    for index, arg in enumerate(args):
        if arg.startswith("factory/cli/tests/"):
            args[index] = str(workspace / arg)
    args.extend(["--junitxml", os.environ["XML_OUTPUT_FILE"]])
    os.chdir(workspace / "factory/cli")

    os.environ["COVERAGE_FILE"] = str(
        Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / ".coverage"
    )
    yield


@pytest.hookimpl(trylast=True)  # noqa: V103
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Assign each collected case to one Bazel shard."""
    count = int(os.environ.get("TEST_TOTAL_SHARDS", "1"))
    if count <= 1:
        return
    index = int(os.environ["TEST_SHARD_INDEX"])
    Path(os.environ["TEST_SHARD_STATUS_FILE"]).touch()
    deselected = [item for offset, item in enumerate(items) if offset % count != index]
    items[:] = items[index::count]
    config.hook.pytest_deselected(items=deselected)
