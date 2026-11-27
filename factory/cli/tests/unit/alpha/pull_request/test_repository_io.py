# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request repository."""

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from fake_tool import PR_ORIGIN as ORIGIN
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import BASE
from unit.alpha.pull_request.pr_test_support import (
    TARGET,
    VISIBILITY_READ,
    World,
    assert_trunk_count,
    base_case,
    branch_cases,
    edit_cases,
    gh_argvs,
    remote_default,
    run_main,
    write_draft,
)

from tbhbdev.alpha import size_gate
from tbhbdev.alpha.pull_request import attribution as pr_attribution
from tbhbdev.alpha.pull_request import repository as pr_repository

if TYPE_CHECKING:
    from pathlib import Path


host_names = pr_test_support.host_names
world = pr_test_support.world


def test_a_draft_without_a_base_takes_the_default_branch(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted = Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None))
    monkeypatch.setattr(size_gate, "size_counts", counted)
    monkeypatch.setattr(pr_attribution, "branch_trailers", Mock(return_value=[]))
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    remote_default(world, tmp_path, "trunk")
    write_draft(world, BASE)
    run_main("check")
    assert_trunk_count(counted, world)


def test_the_default_branch_ignores_a_tag_of_the_same_name(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted = Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None))
    monkeypatch.setattr(size_gate, "size_counts", counted)
    monkeypatch.setattr(pr_attribution, "branch_trailers", Mock(return_value=[]))
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    world.git("tag", "origin/trunk")
    remote_default(world, tmp_path, "trunk")
    assert pr_repository.default_base() == "trunk"


def test_the_remote_default_beats_a_stale_origin_head(
    world: World, tmp_path: Path
) -> None:
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    remote_default(world, tmp_path, "main")
    assert pr_repository.default_base() == "main"


def test_an_unreachable_remote_falls_back_to_origin_head(
    world: World, tmp_path: Path
) -> None:
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    world.git("remote", "set-url", "origin", str(tmp_path / "missing.git"))
    assert pr_repository.default_base() == "trunk"


def test_gh_repo_refuses_a_guessed_default_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_REPO", "other/repo")
    with pytest.raises(SystemExit, match="default branch of GH_REPO"):
        pr_repository.default_base()


def unset_origin_head(world: World, url: str) -> None:
    world.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    world.git("remote", "set-url", "origin", url)


def test_an_unset_origin_head_reads_the_remotes_own_default(
    world: World, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    world.git("init", "-q", "--bare", "-b", "dev", str(remote))
    world.git("push", "-q", str(remote), "HEAD:refs/heads/dev")
    unset_origin_head(world, str(remote))
    assert pr_repository.default_base() == "dev"


def test_create_reads_the_default_branch_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(["trunk", "dev", "other"])
    read = Mock(side_effect=lambda: next(answers))
    monkeypatch.setattr(pr_repository, "default_base", read)
    counted = Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None))
    monkeypatch.setattr(size_gate, "size_counts", counted)
    trailers = Mock(return_value=[])
    monkeypatch.setattr(pr_attribution, "branch_trailers", trailers)
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    write_draft(world)
    assert run_main("create") == 0
    assert read.call_count == 1
    assert counted.call_args.args[0] == "refs/remotes/origin/trunk"
    trailers.assert_called_once_with("trunk")
    _, call = world.calls("gh")
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[-2:] == ["--base", "trunk"]


@pytest.mark.parametrize("kind", ["unreachable", "empty"])
def test_an_unreadable_remote_default_refuses_without_a_guess(
    world: World, tmp_path: Path, kind: str
) -> None:
    remote = tmp_path / "remote.git"
    if kind == "empty":
        world.git("init", "-q", "--bare", "-b", "dev", str(remote))
    unset_origin_head(world, str(remote))
    with pytest.raises(SystemExit, match="cannot find the default branch"):
        pr_repository.default_base()


@pytest.mark.parametrize(
    ("stdout", "rc", "want"),
    [("PRIVATE\n", 0, "PRIVATE"), ("", 0, ""), ("PRIVATE\n", 1, ""), ("", 1, "")],
)
def test_the_visibility_read_names_the_target_and_is_empty_when_it_fails(
    world: World, stdout: str, rc: int, want: str
) -> None:
    world.fake("gh", cases=edit_cases(None, stdout, rc))
    assert pr_repository.repo_visibility(TARGET) == want
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", TARGET, *VISIBILITY_READ]


@pytest.mark.parametrize(
    "target",
    ["tbhb-dev/tbhbdev", "A1-b/.github", "o/a.b_c-d", "o/..a", "o/x", "9/9"],
)
def test_a_well_formed_target_is_read(world: World, target: str) -> None:
    answer: dict[str, object] = {"rc": 0, "stdout": "PRIVATE\n", "stderr": ""}
    world.fake("gh", cases={"repo view": answer})
    assert pr_repository.repo_visibility(target) == "PRIVATE"
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", target, *VISIBILITY_READ]


@pytest.mark.parametrize(
    "target",
    [
        "",
        "tools",
        "o/",
        "/x",
        "o/n/extra",
        "-o/x",
        "o/.",
        "o/..",
        "o/x y",
        "o/x\n",
        "\no/x",
        "o/é",
        "é/x",
        "o x/y",
        "o/x;y",
    ],
)
def test_a_malformed_target_is_empty_without_a_gh_call(
    world: World, target: str
) -> None:
    answer: dict[str, object] = {"rc": 0, "stdout": "PRIVATE\n", "stderr": ""}
    world.fake("gh", cases={"repo view": answer})
    assert pr_repository.repo_visibility(target) == ""
    assert world.calls("gh.repo") == []


@pytest.mark.parametrize(
    ("answer", "want"),
    [
        (base_case(TARGET, "PRIVATE"), "PRIVATE"),
        (base_case(TARGET, "PUBLIC"), "PUBLIC"),
        (base_case(TARGET, ""), ""),
        (base_case(TARGET, None), ""),
        (base_case(TARGET, "PRIVATE", rc=1), ""),
        (base_case("", "PRIVATE"), ""),
        (base_case(None, "PRIVATE"), ""),
        (base_case(7, "PRIVATE"), ""),
        (base_case("a/b/c", "PRIVATE"), ""),
        (base_case("not a name", "PRIVATE"), ""),
        (base_case("o/..", "PRIVATE"), ""),
        ({"rc": 0, "stdout": "[]", "stderr": ""}, ""),
        ({"rc": 0, "stdout": "PRIVATE\n", "stderr": ""}, ""),
        ({"rc": 0, "stdout": '{"visibility": "PRIVATE"}', "stderr": ""}, ""),
        ({"rc": 0, "stdout": '{"nameWithOwner": "o/n"}', "stderr": ""}, ""),
    ],
)
def test_the_base_visibility_counts_only_with_a_parsed_name(
    world: World, answer: dict[str, object], want: str
) -> None:
    world.fake("gh", cases={"repo view": answer})
    assert pr_repository.base_visibility() == want
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", "--json", "nameWithOwner,visibility"]


def test_origin_repository_without_a_push_url_reads_origin(world: World) -> None:
    world.fake("gh", cases=branch_cases())
    assert pr_repository.origin_repository() == "tbhb-dev/example"
    assert gh_argvs(world) == [["repo", "view", ORIGIN, "--json", "nameWithOwner,url"]]
