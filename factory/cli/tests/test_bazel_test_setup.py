"""The default CLI test target blocks I/O and keeps its home in tmp_path."""

# gazelle:include_pytest_conftest false

import ast
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

if "TEST_TMPDIR" not in os.environ:
    pytest.skip("requires the guarded Bazel target", allow_module_level=True)


def assert_subprocess_blocked() -> None:
    with pytest.raises(
        RuntimeError, match=r"CLI unit tests.*subprocess\.Popen\.__init__"
    ):
        subprocess.run([sys.executable, "-c", "pass"], check=True)


assert_subprocess_blocked()
IMPORT_HOME = Path.home()


@pytest.fixture(scope="session")
def session_home() -> Path:
    assert_subprocess_blocked()
    return Path.home()


@pytest.fixture(scope="module")
def module_home() -> Path:
    assert_subprocess_blocked()
    return Path.home()


def test_redirects_home_before_function_setup(
    session_home: Path, module_home: Path
) -> None:
    assert IMPORT_HOME.is_relative_to(os.environ["TEST_TMPDIR"])
    assert session_home == module_home == IMPORT_HOME


def test_blocks_connect() -> None:
    with (
        socket.socket() as client,
        pytest.raises(RuntimeError, match=r"CLI unit tests.*socket\.socket\.connect"),
    ):
        client.connect(("127.0.0.1", 0))


def test_blocks_subprocess() -> None:
    assert_subprocess_blocked()


def test_blocks_sleep() -> None:
    with pytest.raises(RuntimeError, match=r"CLI unit tests.*time\.sleep"):
        time.sleep(0)


@pytest.mark.parametrize(
    "name", [name for name in dir(os) if name.startswith("exec") or name == "system"]
)
def test_blocks_os_calls(name: str) -> None:
    with pytest.raises(RuntimeError, match=rf"CLI unit tests.*os\.{name} is blocked"):
        getattr(os, name)()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_blocks_fork() -> None:
    def fork_and_wait() -> None:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)

    with pytest.raises(RuntimeError, match=r"CLI unit tests.*os\.fork"):
        fork_and_wait()


def test_redirects_home_and_xdg(tmp_path: Path) -> None:
    assert Path.home() == tmp_path
    variables = (
        *(f"XDG_{kind}_HOME" for kind in ("CACHE", "CONFIG", "DATA", "STATE")),
        "XDG_CONFIG_DIRS",
        "XDG_DATA_DIRS",
        "XDG_RUNTIME_DIR",
    )
    for variable in variables:
        assert Path(os.environ[variable]) == tmp_path / variable.lower(), variable


def test_io_baseline_can_only_shrink() -> None:
    source = Path(__file__).with_name("pytest.bzl").read_text(encoding="utf-8")
    assignment = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_IO_TESTS"
            for target in node.targets
        )
    )
    names: list[str] = ast.literal_eval(assignment.value)
    exceptions = set(names)
    assert len(exceptions) == len(names), "Duplicate CLI test I/O exceptions"
    baseline = Path(__file__).with_name("io_test_baseline.txt")
    added = exceptions.difference(baseline.read_text(encoding="utf-8").splitlines())
    assert not added, f"New CLI test I/O exceptions: {sorted(added)}"
