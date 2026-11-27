# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request sizing."""

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from fake_tool import PR_ORIGIN as ORIGIN
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import BASE
from unit.alpha.pull_request.pr_test_support import (
    REAL_GIT,
    REAL_SCC,
    REAL_SIZE_COUNTS,
    REAL_TARGET_IS_ORIGIN,
    World,
    assert_trunk_count,
    branch_cases,
    gh_argvs,
    remote_default,
    run_main,
    write_draft,
)

from tbhbdev.alpha import size_gate
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import tools as pr_tools

host_names = pr_test_support.host_names
world = pr_test_support.world


@pytest.mark.parametrize(
    ("base", "merged_parent"),
    [
        ("parent", None),
        ("parent", "parent"),
        ("parent", "fetched/heads/parent"),
        ("main", None),
    ],
)
def test_size_counts_only_the_drafts_own_lines(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    base: str,
    merged_parent: str | None,
) -> None:
    assert REAL_SCC is not None
    (world.bin / "scc").unlink()
    path = (os.environ["PATH"], str(Path(REAL_SCC).absolute().parent), os.defpath)
    monkeypatch.setenv("PATH", os.pathsep.join(path))
    monkeypatch.setattr(pr_tools, "git", REAL_GIT)
    monkeypatch.setattr(size_gate, "size_counts", REAL_SIZE_COUNTS)

    world.git("switch", "-qc", "parent")
    (world.root / "parent.py").write_text("parent = 1\n")
    world.git("add", "parent.py")
    world.commit("parent work")
    world.git("switch", "-q", "main")
    for name in ("first", "second"):
        (world.root / f"{name}.py").write_text(f"{name} = 1\n")
        world.git("add", f"{name}.py")
        world.commit(f"{name} main change")
    remote_default(world, world.root.parent, "main")
    remote = world.git("remote", "get-url", "origin")
    world.git("push", "-q", remote, f"parent:refs/heads/{merged_parent or 'parent'}")

    world.git("switch", "-qc", "child")
    if merged_parent is not None:
        world.git("merge", "-q", "--no-edit", "parent")
    (world.root / "child.py").write_text("child = 1\n")
    world.git("add", "child.py")
    world.commit("child work")
    write_draft(world, f"---\nbase: {base}\n---\n" + BASE)
    assert run_main("check") == 0
    assert capsys.readouterr() == (
        "1 code lines, limit 400\n1 raw lines, limit 1500\nOK: the draft conforms\n",
        "",
    )


def test_check_counts_an_explicit_base_with_gh_repo(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_default(world, tmp_path, "trunk")
    monkeypatch.setenv("GH_REPO", "tbhb-dev/example")
    monkeypatch.setattr(pr_tools, "git", REAL_GIT)
    counter = Mock(return_value=size_gate.SizeCounts(401, 400, 401, 1500, "code"))
    monkeypatch.setattr(size_gate, "size_counts", counter)
    write_draft(world, "---\nbase: trunk\n---\n" + BASE)

    assert run_main("check") == 1
    assert_trunk_count(counter, world)


@pytest.mark.parametrize("local_mirror", [False, True], ids=["github", "local-mirror"])
def test_create_counts_against_origin_fetch_repository(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_mirror: bool
) -> None:
    push_repo = "example-org/fork"
    if local_mirror:
        remote_default(world, tmp_path, "main")
        push_repo = "tbhb-dev/example"
    push_url = f"https://github.com/{push_repo}.git"
    world.git("config", "remote.origin.pushurl", push_url)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(pr_repository, "target_is_origin", REAL_TARGET_IS_ORIGIN)
    counter = Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None))
    monkeypatch.setattr(size_gate, "size_counts", counter)
    world.fake(
        "gh",
        cases={
            f"repo view {push_url}": {
                "rc": 0,
                "stdout": json.dumps(
                    {
                        "nameWithOwner": push_repo,
                        "url": f"https://github.com/{push_repo}",
                    }
                ),
                "stderr": "",
            },
            "repo view /": {"rc": 1, "stdout": "", "stderr": "invalid repository"},
            **branch_cases(),
        },
    )
    write_draft(world)

    assert run_main("create", "--dry-run") == 0
    counter.assert_called_once()
    assert counter.call_args.args == ("refs/remotes/origin/main", "HEAD")
    lookup_url = push_url if local_mirror else ORIGIN
    assert [argv for argv in gh_argvs(world) if "nameWithOwner,url" in argv] == [
        ["repo", "view", lookup_url, "--json", "nameWithOwner,url"],
    ]
