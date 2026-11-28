"""Exercise remote cleanup and retention on scratch run files."""

from __future__ import annotations

import json
import subprocess
lazy from pathlib import Path

import pytest
from agent_driver_example import write_standalone_fake
from remote_script_example import cleanup, clone_in, execute, no_session

from tbhbdev.alpha.agent.decisions import remote_cleanup


@pytest.mark.parametrize(
    "step", ["worktree", "sample.phase", "sample.rc", "sample.owner"]
)
def test_clean_retries_each_step(host: Path, step: str) -> None:
    clone_in(host)
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.phase").write_text("ended\n")
    (state / "sample.rc").write_text("0\n")
    marker = host / "failed"
    write_standalone_fake(
        host / "bin/mv",
        "import os, sys\nfrom pathlib import Path\n"
        f"marker = Path({str(marker)!r})\nargs = sys.argv[1:]\nstep = {step!r}\n"
        "match = step in (Path(args[-1]).name, Path(args[0]).name)\n"
        "if match and not marker.exists():\n"
        "    marker.write_text('failed')\n    sys.exit(74)\n"
        "os.execv('/bin/mv', ['/bin/mv', *args])\n",
    )
    assert execute(cleanup()).returncode == 74
    assert execute(cleanup()).returncode == 0
    trash = state / "trash/stamp-sample"
    assert (trash / "worktree/agent-file").read_text() == "preserve"
    assert (trash / "sample.owner").read_text() == "example-owner\n"
    assert not list(state.glob("sample.*"))


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
@pytest.mark.parametrize("component", ["clone", ".worktrees", "agent-sample"])
def test_clean_refuses_unsafe_run_clone(host: Path, kind: str, component: str) -> None:
    clone = clone_in(host)
    no_session(host)
    tree = {
        "clone": clone,
        ".worktrees": clone / ".worktrees",
        "agent-sample": clone / ".worktrees/agent-sample",
    }[component]
    tree.rename(host / "saved-clone")
    if kind == "file":
        tree.write_text("preserve")
    else:
        tree.symlink_to(
            host / ("saved-clone" if kind == "symlink" else "missing"),
            target_is_directory=True,
        )
    result = execute(cleanup())
    assert result.returncode == 65
    reason = "is not a directory" if kind == "file" else "is a symlink"
    assert result.stderr == f"clean refused: {tree} {reason}\n".encode()
    assert next((host / "saved-clone").rglob("agent-file")).read_text() == "preserve"


def test_clean_preserves_an_ownerless_detached_group(host: Path) -> None:
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.owner").unlink()
    group = state / "sample.pgid"
    (state / "sample.rc").write_text("137\n")
    (state / "sample.build").write_text("example record")
    (state / "sample.cli.tar").write_text("example archive")
    folder = state / "sample.cli"
    folder.mkdir()
    (folder / "example.txt").write_text("example build")
    with subprocess.Popen(["sleep", "30"], start_new_session=True) as process:
        try:
            group.write_text(json.dumps({"pgid": process.pid}))
            assert execute(cleanup()).returncode == 1
            assert group.exists()
            assert folder.is_dir()
            assert (state / "sample.cli.tar").is_file()
            assert (state / "sample.build").is_file()
            assert not (state / "sample.owner").exists()
        finally:
            process.terminate()
    assert execute(cleanup()).returncode == 0
    assert not list(state.glob("sample.*"))
    trash = state / "trash/stamp-sample"
    assert (trash / "sample.cli/example.txt").read_text() == "example build"
    assert (trash / "sample.cli.tar").read_text() == "example archive"
    assert (trash / "sample.build").read_text() == "example record"


def test_clean_checks_flags_fallback_and_missing_clone(host: Path) -> None:
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.rc.fallback").write_text("")
    assert execute(cleanup()).returncode == 65
    assert execute(cleanup(force=True)).returncode == 0
    assert (state / "trash/stamp-sample/sample.rc.fallback").is_file()


@pytest.mark.parametrize("destination", ["worktree", "sample.phase"])
def test_clean_preserves_conflicting_trash(host: Path, destination: str) -> None:
    clone_in(host)
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.phase").write_text("ended\n")
    trash = state / "trash/stamp-sample"
    trash.mkdir(parents=True)
    conflict = trash / destination
    conflict.write_text("preserve existing trash")
    result = execute(cleanup())
    assert result.returncode == 65
    assert conflict.read_text() == "preserve existing trash"
    assert (state / "sample.owner").read_text() == "example-owner\n"


def test_keep_is_idempotent_with_matching_owner(host: Path) -> None:
    assert execute(remote_cleanup.keep("sample")).returncode == 0
    assert execute(remote_cleanup.keep("sample")).returncode == 0
    assert (host / "state/tbhbdev/agent-runs/sample.keep").read_text() == ""
