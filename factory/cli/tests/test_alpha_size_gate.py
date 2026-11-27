"""Size counter and review reads use fakes; no test runs Bazel."""

import argparse
import json
import os
import subprocess
from dataclasses import replace
from unittest.mock import Mock, call
lazy from pathlib import Path

import pytest

from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha import size_gate as sg

BASE_COMMIT = "a" * 40
HEAD_COMMIT = "b" * 40
DIFF_RANGE = f"{BASE_COMMIT}...{HEAD_COMMIT}"
GOOD = {
    "code_lines": 400,
    "code_limit": 400,
    "raw_lines": 1500,
    "raw_limit": 1500,
    "failed": None,
    "diff_range": DIFF_RANGE,
    "merge_tree_command": "",
}


@pytest.mark.parametrize(
    ("source_lines", "yaml_lines", "expected"),
    [
        (375, 2, sg.SizeCounts(411, 400, 416, 1500, "code")),
        (364, 1097, sg.SizeCounts(400, 400, 1500, 1500, None)),
        (364, 1098, sg.SizeCounts(400, 400, 1501, 1500, "raw")),
        (365, 1098, sg.SizeCounts(401, 400, 1502, 1500, "code")),
    ],
)
def test_size_count_matches_rules_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_lines: int,
    yaml_lines: int,
    expected: sg.SizeCounts,
) -> None:
    """CI counts BUILD code too, while exempting prose and generated files."""
    monkeypatch.chdir(tmp_path)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "base",
        "--allow-empty",
    )
    base = git("rev-parse", "HEAD")
    for name, content in {
        "source.py": "x = 1\n" * source_lines + "# comment\n\n",
        "BUILD": "x = 1\n" * 9,
        "BUILD.bazel": "x = 1\n" * 9,
        "MODULE.bazel": "x = 1\n" * 9,
        "tool.bzl": "x = 1\n" * 9,
        "config.yaml": "key: value\n" * yaml_lines,
        "readme.md": "prose\n" * 500,
        "uv.lock": "x = 1\n" * 500,
        "vendor/a.py": "x = 1\n" * 500,
        "tests/testdata/a.py": "x = 1\n" * 500,
        "generated.py": "# Code generated; DO NOT EDIT.\n" + "x = 1\n" * 500,
        ".gitattributes": "marked.py linguist-generated\n",
        "marked.py": "x = 1\n" * 500,
        ".github/pr-size-exempt.txt": "# Local exemptions\n\nlocal/*",
        "local/a.py": "x = 1\n" * 500,
        "binary.py": "\0binary\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git("add", ".")
    git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "change",
    )
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", "--detach", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    monkeypatch.chdir(linked / ".github")
    head = git("rev-parse", "HEAD")
    runner = sg.subprocess.run

    def without_bazel(
        argv: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert "bazel" not in argv[0], "the controller must not start Bazel"
        return runner(argv, capture_output=capture_output, text=text, check=check)

    monkeypatch.setattr(sg.subprocess, "run", without_bazel)
    assert sg.size_counts(base, "HEAD") == replace(
        expected, diff_range=f"{base}...{head}"
    )


@pytest.mark.parametrize(
    ("name", "history", "expected"),
    [
        (
            "source.py",
            (
                "parent = 1\n",
                "main = 1\n",
                "parent = 1\nmain = 1\nresolution = 1\n",
                "child = 1\n" * 400,
            ),
            sg.SizeCounts(402, 400, 402, 1500, "code"),
        ),
        (
            "config.toml",
            (
                'value = """\nparent\n"""\n',
                'value = """\nmain\n"""\n',
                'value = """\n=======\nmain\n"""\n',
                "# child\n" * 1500,
            ),
            sg.SizeCounts(0, 400, 1501, 1500, "raw"),
        ),
    ],
    ids=["code-limit", "literal-separator"],
)
def test_stacked_count_includes_child_lines_in_a_resolved_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    history: tuple[str, str, str, str],
    expected: sg.SizeCounts,
) -> None:
    monkeypatch.chdir(tmp_path)
    parent, main, resolved, additions = history

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.PIPE, text=True
        ).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "-qm", "root", "--allow-empty")
    source = tmp_path / name
    git("switch", "-qc", "parent")
    source.write_text(parent)
    git("add", name)
    git("commit", "-qm", "parent work")
    git("switch", "-q", "main")
    source.write_text(main)
    git("add", name)
    git("commit", "-qm", "main work")

    git("switch", "-qc", "child", "parent")
    with pytest.raises(subprocess.CalledProcessError) as conflict:
        git("merge", "--no-edit", "main")
    assert conflict.value.returncode == 1
    source.write_text(resolved)
    git("add", name)
    git("commit", "-qm", "merge main")
    with source.open("a") as out:
        out.write(additions)
    git("commit", "-qam", "child work")

    assert sg.size_counts("parent", "HEAD", default_ref="main") == replace(
        expected, diff_range=f"{git('rev-parse', 'main')}..{git('rev-parse', 'HEAD')}"
    )


@pytest.mark.parametrize("failed", [None, "code", "raw"])
@pytest.mark.parametrize("tools", [{}, {"scc": "/tools/scc", "git": "/tools/git"}])
@pytest.mark.parametrize("default_ref", [None, "origin/trunk"])
def test_counts(
    monkeypatch: pytest.MonkeyPatch,
    failed: str | None,
    tools: dict[str, str],
    default_ref: str | None,
) -> None:
    runner = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({**GOOD, "failed": failed}), ""
        )
    )
    monkeypatch.setattr(sg.subprocess, "run", runner)
    assert sg.size_counts(
        "origin/main", "HEAD", default_ref=default_ref, **tools
    ) == sg.SizeCounts(400, 400, 1500, 1500, failed, diff_range=DIFF_RANGE)
    assert runner.call_count == 1
    assert runner.call_args.args == (
        [
            "bash",
            str(sg.DATA / "pr-size.sh"),
            "origin/main",
            "HEAD",
            tools.get("scc", "scc"),
            tools.get("git", "git"),
            str(sg.DATA / "pr-size-exempt.txt"),
            default_ref or "",
        ],
    )
    assert runner.call_args.kwargs == {
        "capture_output": True,
        "text": True,
        "check": False,
    }


@pytest.mark.parametrize("error", [OSError("no bash"), None])
@pytest.mark.parametrize("stderr", ["missing target\n", ""])
def test_counter_errors(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None, stderr: str
) -> None:
    runner = Mock(
        side_effect=error, return_value=subprocess.CompletedProcess([], 2, "", stderr)
    )
    monkeypatch.setattr(sg.subprocess, "run", runner)
    reason = "no bash" if error else (stderr.strip() or "counter exited 2")
    with pytest.raises(sg.SizeCountError, match=reason):
        sg.size_counts("base", "head")


@pytest.mark.parametrize("stdout", ["", "{}\n{}", "not JSON"])
def test_bad_json(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(
        sg.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")),
    )
    with pytest.raises(sg.SizeCountError, match=r"^counter returned invalid JSON$"):
        sg.size_counts("base", "head")


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        *[
            {key: value for key, value in GOOD.items() if key != missing}
            for missing in GOOD
        ],
        *[
            {**GOOD, key: value}
            for key in sg.COUNT_KEYS
            for value in (True, None, "1", 1.0, [0])
        ],
        *[{**GOOD, "failed": value} for value in (True, 0, [0], {"x": 0}, "other")],
    ],
)
def test_bad_counts(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    monkeypatch.setattr(
        sg.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")),
    )
    with pytest.raises(
        sg.SizeCountError, match=r"^counter returned invalid size counts$"
    ):
        sg.size_counts("base", "head")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, False),
        ({}, False),
        (
            {
                "sha": "other",
                "statuses": [{"context": "size-review", "state": "success"}],
            },
            False,
        ),
        ({"sha": "head"}, False),
        ({"sha": "head", "statuses": {}}, False),
        ({"sha": "head", "statuses": []}, False),
        *[
            ({"sha": "head", "statuses": [status]}, False)
            for status in (
                None,
                {"state": "success"},
                {"context": "other", "state": "success"},
                {"context": "size-review", "state": "pending"},
                {"context": "size-review", "state": "failure"},
            )
        ],
        (
            {
                "sha": "head",
                "statuses": [{"context": "size-review", "state": "success"}],
            },
            True,
        ),
    ],
)
def test_review(monkeypatch: pytest.MonkeyPatch, data: object, expected: bool) -> None:
    reader = Mock(return_value=data)
    monkeypatch.setattr(sg, "gh_api", reader)
    assert sg.has_size_review_success("owner/repo", "head") is expected
    reader.assert_called_once_with(
        "GET", "repos/owner/repo/commits/head/status", env=os.environ
    )


@pytest.mark.parametrize(
    ("counts", "problem"),
    [
        (sg.SizeCounts(400, 400, 1500, 1500, None), ""),
        (sg.SizeCounts(401, 400, 1400, 1500, "code"), "401 lines, limit 400"),
        (sg.SizeCounts(401, 400, 1501, 1500, "code"), "401 lines, limit 400"),
        (sg.SizeCounts(100, 400, 1501, 1500, "raw"), "1501 lines, limit 1500"),
    ],
)
@pytest.mark.usefixtures("pr_tools")
def test_pr_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    counts: sg.SizeCounts,
    problem: str,
    size_launcher: Mock,
) -> None:
    checked = pr.Checked([])
    lint = Mock(return_value=checked)
    monkeypatch.setattr(pr, "read_checked", lint)
    counter = Mock(return_value=counts)
    monkeypatch.setattr(sg, "size_counts", counter)
    monkeypatch.setattr(pr, "git", git_exits())
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    assert pr.command(
        argparse.Namespace(command="check", draft="draft.md"), {}
    ) == bool(problem)
    lint.assert_called_once_with("draft.md")
    counter.assert_called_once_with(
        BASE_COMMIT,
        HEAD_COMMIT,
        default_ref=None,
        scc="/tools/scc",
        git="/tools/git",
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(
        f"{counts.code_lines} code lines, limit 400\n"
        f"{counts.raw_lines} raw lines, limit 1500\n"
    )
    if problem:
        size_launcher.assert_called_once_with(
            "owner/repo", "main", HEAD_COMMIT, counts, {}
        )
        assert checked.problems == [(1, f"{problem}: split into stacked PRs")]
        assert captured.out.endswith(f"draft.md:1: {problem}: split into stacked PRs\n")
    else:
        size_launcher.assert_not_called()
        assert captured.out.endswith("OK: the draft conforms\n")


@pytest.mark.usefixtures("pr_tools")
def test_pr_check_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pr, "git", Mock(return_value=(0, "")))
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    monkeypatch.setattr(pr, "read_checked", Mock(return_value=pr.Checked([])))
    monkeypatch.setattr(
        sg, "size_counts", Mock(side_effect=sg.SizeCountError("missing target"))
    )
    assert pr.command(argparse.Namespace(command="check", draft="draft.md"), {}) == 0
    captured = capsys.readouterr()
    assert captured.err == "size count unavailable: missing target\n"
    assert captured.out == "OK: the draft conforms\n"


REAL_TARGET_IS_ORIGIN = pr.target_is_origin


@pytest.fixture
def pr_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def paths(names: tuple[str, ...], **_options: object) -> dict[str, str]:
        return {name: f"/tools/{name}" for name in names}

    monkeypatch.setattr(pr, "find_tools", Mock(side_effect=paths))
    monkeypatch.setattr(pr, "origin_repository", Mock(return_value="owner/repo"))
    monkeypatch.setattr(sg, "size_review_state", Mock(return_value=""))


@pytest.fixture
def size_launcher(monkeypatch: pytest.MonkeyPatch) -> Mock:
    launcher = Mock()
    monkeypatch.setattr(pr, "start_size_review", launcher)
    return launcher


@pytest.fixture(autouse=True)
def gh_targets_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr, "target_is_origin", Mock(return_value=True))


FETCH = ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--refmap="]


def git_exits(code: int = 0) -> Mock:
    """A git mock that returns pinned commit IDs."""

    def resolve(*args: str) -> tuple[int, str]:
        return code, HEAD_COMMIT if args[-1] == "HEAD" else BASE_COMMIT

    return Mock(side_effect=resolve)


@pytest.mark.parametrize("failure", ["count", "over", "base", "override"])
@pytest.mark.usefixtures("pr_tools")
def test_pr_create_size(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    size_launcher: Mock,
) -> None:
    counts = sg.SizeCounts(401, 400, 500, 1500, "code")
    counter = Mock(return_value=counts)
    if failure == "count":
        counter = Mock(side_effect=sg.SizeCountError("missing target"))
    monkeypatch.setattr(sg, "size_counts", counter)
    git = git_exits(int(failure == "base"))
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    monkeypatch.setattr(pr, "read_checked", Mock(return_value=pr.Checked([])))
    monkeypatch.setattr(pr, "branch_trailers", Mock(return_value=[]))
    monkeypatch.setattr(pr, "post_text", Mock())
    monkeypatch.setattr(pr, "base_visibility", Mock(return_value="public"))
    post = Mock(return_value=0)
    monkeypatch.setattr(pr, "create_pr", post)
    if failure == "override":
        monkeypatch.setattr(sg, "size_review_state", Mock(return_value="success"))
    assert pr.command(
        argparse.Namespace(command="create", draft="d.md", dry_run=False), {}
    ) == (failure == "over")
    captured = capsys.readouterr()
    assert post.called is (failure != "over")
    if failure == "over":
        size_launcher.assert_called_once_with(
            "owner/repo", "main", HEAD_COMMIT, counts, {}
        )
        assert captured.out == "d.md:1: 401 lines, limit 400: split into stacked PRs\n"
    elif failure != "override":
        assert captured.err.startswith("size count unavailable: ")
    if failure != "over":
        size_launcher.assert_not_called()
    fetched = [*FETCH, "origin", "+refs/heads/main:refs/remotes/origin/main"]
    assert call(*fetched) in git.call_args_list
    if failure != "base":
        counter.assert_called_once_with(
            BASE_COMMIT,
            HEAD_COMMIT,
            default_ref=None,
            scc="/tools/scc",
            git="/tools/git",
        )


def test_both_stacked_refs_are_refreshed_without_tags_then_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = Mock(return_value=sg.SizeCounts(5, 400, 9, 1500, None))
    monkeypatch.setattr(sg, "size_counts", counter)
    git = git_exits()
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "default_base", Mock(return_value="main"))
    find = Mock(return_value="/tools/scc")
    monkeypatch.setattr(pr, "find_tool", find)
    checked = pr.Checked([], meta={"base": "topic"})
    pr.check_size(argparse.Namespace(command="create", draft="d.md"), checked, {})
    assert checked.problems == []
    assert git.call_args_list[0] == call(
        *FETCH,
        "origin",
        "+refs/heads/topic:refs/remotes/origin/topic",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    counter.assert_called_once_with(
        BASE_COMMIT,
        HEAD_COMMIT,
        default_ref="refs/remotes/origin/main",
        scc="/tools/scc",
        git="git",
    )
    find.assert_called_once_with("scc")


def test_a_failed_fetch_warns_and_never_counts_a_stale_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counter = Mock(return_value=sg.SizeCounts(411, 400, 500, 1500, "code"))
    monkeypatch.setattr(sg, "size_counts", counter)

    git = git_exits(1)
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    checked = pr.Checked([])
    pr.check_size(argparse.Namespace(command="create", draft="d.md"), checked, {})
    assert checked.problems == []
    assert capsys.readouterr().err == "size count unavailable: cannot fetch main\n"
    counter.assert_not_called()
    assert [c.args[0] for c in git.call_args_list] == ["fetch", "fetch"]
    assert "--refmap=" in git.call_args_list[0].args


def test_create_warns_and_never_counts_when_gh_targets_another_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counter = Mock(return_value=sg.SizeCounts(411, 400, 500, 1500, "code"))
    monkeypatch.setattr(sg, "size_counts", counter)
    git = Mock(return_value=(0, ""))
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "target_is_origin", Mock(return_value=False))
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    checked = pr.Checked([])
    pr.check_size(argparse.Namespace(command="create", draft="d.md"), checked, {})
    assert checked.problems == []
    assert capsys.readouterr().err == (
        "size count unavailable: gh may target a repository other than origin\n"
    )
    counter.assert_not_called()
    git.assert_not_called()


@pytest.mark.parametrize(
    ("view", "origin", "expected"),
    [
        ({"nameWithOwner": "o/r"}, "O/R", True),
        ({"nameWithOwner": "parent/r"}, "o/r", False),
        ({}, "o/r", False),
        (None, "o/r", False),
        ({"nameWithOwner": "o/r"}, SystemExit("cannot read origin"), False),
    ],
)
def test_target_is_origin(
    monkeypatch: pytest.MonkeyPatch,
    view: object,
    origin: str | BaseException,
    expected: bool,
) -> None:
    monkeypatch.setattr(pr, "find_tool", Mock(return_value="gh"))
    monkeypatch.setattr(pr, "run", Mock())
    monkeypatch.setattr(pr, "gh_data", Mock(return_value=view))
    monkeypatch.setattr(pr, "origin_repository", Mock(side_effect=[origin]))
    monkeypatch.delenv("GH_REPO", raising=False)
    assert REAL_TARGET_IS_ORIGIN() is expected


def test_gh_repo_env_means_the_target_is_not_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock()
    monkeypatch.setattr(pr, "run", run)
    monkeypatch.setenv("GH_REPO", "parent/r")
    assert REAL_TARGET_IS_ORIGIN() is False
    run.assert_not_called()


def test_a_dry_run_fetches_nothing_and_warns_on_an_excess(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = sg.SizeCounts(401, 400, 500, 1500, "code")
    monkeypatch.setattr(sg, "size_counts", Mock(return_value=counts))
    git = git_exits()
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    monkeypatch.setattr(pr, "find_tool", Mock(return_value="/tools/scc"))
    checked = pr.Checked([])
    args = argparse.Namespace(command="create", draft="d.md", dry_run=True)
    pr.check_size(args, checked, {})
    captured = capsys.readouterr()
    assert checked.problems == []
    assert captured.out == ""
    assert (
        captured.err
        == "warning: d.md:1: 401 lines, limit 400: split into stacked PRs\n"
    )
    assert all(c.args[0] != "fetch" for c in git.call_args_list)
    failing = Mock(return_value=(1, ""))
    monkeypatch.setattr(pr, "git", failing)
    pr.check_size(args, checked, {})
    assert capsys.readouterr().err == (
        "size count unavailable: base branch refs/remotes/origin/main not found\n"
    )
    assert all(c.args[0] != "fetch" for c in failing.call_args_list)
