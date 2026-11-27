# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Set up pytest's working directory and pinned tools in Bazel runfiles."""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


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
