# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request prose."""

import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from fake_tool import (
    write_fake,
)
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import BASE, replace_once
from unit.alpha.pull_request.pr_test_support import (
    FIXTURE,
    WORKSPACE,
    World,
    copy_pr_baseline,
)

from tbhbdev.alpha.pull_request import body as pr_body
from tbhbdev.alpha.pull_request import draft_io as pr_draft_io
from tbhbdev.alpha.pull_request import prose as pr_prose

if TYPE_CHECKING:
    from pathlib import Path


draft_world = pr_test_support.draft_world
host_names = pr_test_support.host_names
world = pr_test_support.world


def test_title_goes_to_cog_on_stdin(draft_world: World) -> None:
    assert pr_prose.title_problems("feat: x") == []
    call = draft_world.calls("cog")[0]
    assert call["argv"] == ["verify", "--file", "-"]
    assert call["stdin"] == "feat: x"
    assert call["cwd"] == str(draft_world.root.resolve())


MISSING = (
    "title is not a Conventional Commit header (cog verify): Error: Missing commit type"
)


def test_title_cog_failure_is_reported(draft_world: World) -> None:
    draft_world.fake("cog", rc=1, stderr="Error: Missing\n\n  commit type\n")
    assert pr_prose.title_problems("Add thing") == [MISSING]


@pytest.mark.usefixtures("draft_world")
def test_title_length() -> None:
    assert pr_prose.MAX_TITLE == 64
    assert pr_prose.title_problems("feat: " + "x" * 58) == []
    assert pr_prose.title_problems("feat: " + "x" * 59) == [
        "title is 65 characters; the limit is 64"
    ]


def test_title_problem_lands_on_the_title_line(draft_world: World) -> None:
    draft_world.fake("cog", rc=1, stderr="bad")
    found = pr_draft_io.check_text("---\nbase: main\n---\n\n" + BASE).problems
    assert found == [(5, "title is not a Conventional Commit header (cog verify): bad")]


@pytest.mark.skipif(shutil.which("cog") is None, reason="cog is not installed")
@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("feat(cli)!: add x", True),
        ("fix: y", True),
        ("Add thing", False),
        ("wip: x", False),
    ],
)
def test_real_cog(
    title: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ok: bool
) -> None:
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "example@example.test"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    assert (pr_prose.title_problems(title) == []) is ok


def lint_checked(world: World, text: str) -> list[pr_body.Problem]:
    del world
    return pr_draft_io.check_text(text).problems


def test_lint_runs_each_tool_in_a_mirrored_tree(draft_world: World) -> None:
    text = "---\nbase: main\n---\n\n" + BASE
    assert lint_checked(draft_world, text) == []
    rendered = text.split("---\n\n", 1)[1]
    for name, argv in (
        ("vale", ["--output=JSON", ".github/PULL_REQUEST_DRAFT.md"]),
        (
            "cspell",
            [
                "--config",
                ".cspell.jsonc",
                "--no-summary",
                "--no-progress",
                ".github/PULL_REQUEST_DRAFT.md",
            ],
        ),
        ("rumdl", ["check", ".github/PULL_REQUEST_DRAFT.md"]),
    ):
        (call,) = draft_world.calls(name)
        assert call["argv"] == argv
        assert call["draft"] == rendered
        assert call["stdin"] == ""
        assert call["links"] == {
            config: str(draft_world.root / config) for config in pr_prose.CONFIGS
        }
        assert call["cwd"] != str(draft_world.root)


def test_lint_maps_lines_back_to_the_draft(draft_world: World) -> None:
    draft_world.fake(
        "vale",
        rc=1,
        stdout=json.dumps(
            {
                "f": [
                    {"Check": "Google.We", "Message": "no we", "Line": 3},
                    {
                        "Check": "project.HeadingTitleCase",
                        "Message": "Title",
                        "Line": 1,
                    },
                    {
                        "Check": "project.HeadingTitleCase",
                        "Message": "Title",
                        "Line": 7,
                    },
                ]
            }
        ),
    )
    draft_world.fake(
        "cspell",
        rc=1,
        stdout=".github/PULL_REQUEST_DRAFT.md:5:2 - Unknown word (zzz)\n",
    )
    draft_world.fake(
        "rumdl",
        rc=1,
        stdout=(
            ".github/PULL_REQUEST_DRAFT.md:9:1: [MD022] spacing [*]\n\n"
            "Issues: Found 1 issue\n"
        ),
    )
    found = lint_checked(draft_world, "---\nbase: main\n---\n\n" + BASE)
    assert found == [
        (7, "vale Google.We: no we"),
        (9, "cspell: Unknown word (zzz)"),
        (11, "vale project.HeadingTitleCase: Title"),
        (13, "rumdl: [MD022] spacing [*]"),
    ]


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr"),
    [
        (2, "", "Configuration Error: bad"),
        (1, "{}", ""),
        (3, "", "killed"),
    ],
)
def test_lint_tool_failure_is_a_problem(
    draft_world: World, rc: int, stdout: str, stderr: str
) -> None:
    draft_world.fake("vale", rc=rc, stdout=stdout, stderr=stderr)
    found = lint_checked(draft_world, BASE)
    message = stderr or stdout
    assert found == [(1, f"vale failed (exit {rc}): {message}")]


def test_lint_failure_message_prefers_stderr(draft_world: World) -> None:
    draft_world.fake("cspell", rc=2, stdout="out", stderr="err  text")
    assert lint_checked(draft_world, BASE) == [(1, "cspell failed (exit 2): err text")]


@pytest.mark.parametrize("rc", [0, 1])
def test_lint_refuses_invalid_vale_json_on_success(draft_world: World, rc: int) -> None:
    draft_world.fake("vale", rc=rc, stdout="{")
    assert lint_checked(draft_world, BASE) == [
        (1, f"vale failed (exit {rc}): invalid JSON")
    ]


def test_lint_failure_with_findings_reports_the_failure_only(
    draft_world: World,
) -> None:
    draft_world.fake("rumdl", rc=2, stdout="a.md:3:1: [MD001] x\n", stderr="boom")
    assert lint_checked(draft_world, BASE) == [(1, "rumdl failed (exit 2): boom")]


def test_lint_needs_every_tool_and_config(draft_world: World) -> None:
    (draft_world.bin / "rumdl").unlink()
    with pytest.raises(SystemExit, match="rumdl not found"):
        lint_checked(draft_world, BASE)
    draft_world.fake("rumdl")
    (draft_world.root / ".rumdl.toml").unlink()
    with pytest.raises(SystemExit, match=r"missing lint config \.rumdl\.toml in "):
        lint_checked(draft_world, BASE)


VALE_INI = """StylesPath = .vale
Packages = https://example.com/Google.zip, \\
  https://example.com/write-good.zip

[*.md]
BasedOnStyles = Vale, Google,  write-good, project

[tmp/**]
BasedOnStyles =

[*.txt]
BasedOnStyles = Google, extra
"""


def styled(world: World, *present: str) -> None:
    """A .vale.ini naming four styles, with only ``present`` synced."""
    (world.root / ".vale.ini").write_text(VALE_INI)
    (world.root / ".vale").unlink()
    for name in present:
        (world.root / ".vale" / name).mkdir(parents=True)


SYNCED = '("Google", "write-good", "extra")'
SYNCS = """import os, sys
open(os.path.join(os.path.dirname(__file__), "bazelisk.log"), "a").write(
    " ".join(sys.argv[1:]) + "|" + os.getcwd() + "\\n"
)
if sys.argv[1:] == ["query", "//:vale_sync"]:
    print("//:vale_sync")
    sys.exit(0)
for name in SYNCED:
    os.makedirs(os.path.join(".vale", name), exist_ok=True)
"""


def test_vale_styles_names_each_style_once(draft_world: World) -> None:
    styled(draft_world)
    assert pr_prose.vale_styles(draft_world.root) == [
        "Google",
        "extra",
        "project",
        "write-good",
    ]


def test_vale_styles_split_at_the_first_equals(draft_world: World) -> None:
    # Vale's ini parser splits a key from its value at the first "=".
    (draft_world.root / ".vale.ini").write_text("BasedOnStyles = odd=name\n")
    assert pr_prose.vale_styles(draft_world.root) == ["odd=name"]


def test_synced_styles_need_no_sync(draft_world: World) -> None:
    styled(draft_world, "Google", "extra", "project", "write-good")
    assert lint_checked(draft_world, BASE) == []
    assert not (draft_world.bin / "bazelisk.log").exists()
    assert len(draft_world.calls("vale")) == 1


def test_missing_styles_sync_once_then_lint(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    styled(world, "project")
    write_fake(world.bin / "bazelisk", SYNCS.replace("SYNCED", SYNCED))
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root / "sub")  # the sync runs at the top, not here
    assert lint_checked(world, BASE) == []
    log = (world.bin / "bazelisk.log").read_text().splitlines()
    assert log == [
        f"query //:vale_sync|{world.root.resolve()}",
        f"run //:vale_sync|{world.root.resolve()}",
    ]
    assert len(world.calls("vale")) == 1


@pytest.mark.parametrize(
    ("rc", "stdout"), [(0, ""), (0, "//:vale_sync_other\n"), (7, "//:vale_sync\n")]
)
def test_missing_sync_target_is_one_problem(
    draft_world: World, rc: int, stdout: str
) -> None:
    styled(draft_world, "project", "extra")
    draft_world.fake("bazelisk", rc=rc, stdout=stdout)
    assert lint_checked(draft_world, BASE) == [
        (
            1,
            (
                "vale: vale styles missing from .vale/: Google, write-good; "
                "missing Bazel target //:vale_sync"
            ),
        )
    ]
    (call,) = draft_world.calls("bazelisk")
    assert call["argv"] == ["query", "//:vale_sync"]
    assert call["cwd"] == str(draft_world.root.resolve())
    assert draft_world.calls("vale") == []
    assert len(draft_world.calls("cspell")) == 1


@pytest.mark.parametrize(
    ("stdout", "stderr", "why"),
    [("out", "no  such target", "no such target"), ("out  text", "", "out text")],
)
def test_failed_sync_is_one_problem(
    draft_world: World, stdout: str, stderr: str, why: str
) -> None:
    styled(draft_world, "project", "extra")
    draft_world.fake(
        "bazelisk",
        rc=3,
        stdout=stdout,
        stderr=stderr,
        cases={"query": {"rc": 0, "stdout": "//:vale_sync\n", "stderr": ""}},
    )
    found = lint_checked(draft_world, BASE)
    assert found == [
        (
            1,
            (
                "vale: vale styles missing from .vale/: Google, write-good; "
                f"`bazelisk run //:vale_sync` exited 3: {why}"
            ),
        )
    ]
    query, call = draft_world.calls("bazelisk")
    assert query["argv"] == ["query", "//:vale_sync"]
    assert call["argv"] == ["run", "//:vale_sync"]
    assert draft_world.calls("vale") == []
    assert len(draft_world.calls("cspell")) == 1


def test_sync_that_leaves_styles_missing_is_a_problem(draft_world: World) -> None:
    styled(draft_world, "project")
    # Exit 0, but it only syncs Google.
    write_fake(draft_world.bin / "bazelisk", SYNCS.replace("SYNCED", '("Google",)'))
    found = lint_checked(draft_world, BASE)
    assert found == [
        (
            1,
            (
                "vale: vale styles missing from .vale/: extra, write-good; "
                "`bazelisk run //:vale_sync` exited 0: "
            ),
        )
    ]


def test_missing_bazelisk_stops_the_check(draft_world: World) -> None:
    styled(draft_world, "project")
    (draft_world.bin / "bazelisk").unlink()
    with pytest.raises(SystemExit, match="bazelisk not found"):
        lint_checked(draft_world, BASE)


def real_lint_ready() -> bool:
    names = ("vale", "cspell", "rumdl", "cog")
    configs = all((WORKSPACE / name).exists() for name in pr_prose.CONFIGS)
    styles = (WORKSPACE / ".vale" / "Google").is_dir()
    return configs and styles and all(shutil.which(name) for name in names)


@pytest.mark.skipif(
    not real_lint_ready(), reason="linters or their configs are missing"
)
class TestRealLinters:
    @pytest.fixture(autouse=True)
    def _real_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "lint-repo"
        root.mkdir()
        for name in [key for key in os.environ if key.startswith("GIT_")]:
            monkeypatch.delenv(name, raising=False)
        for variable in (
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            monkeypatch.setenv(variable, str(tmp_path / variable.lower()))
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Example"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "example@example.test"],
            cwd=root,
            check=True,
        )
        for name in pr_prose.CONFIGS:
            source = WORKSPACE / name
            target = root / name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy(source, target)
        copy_pr_baseline(root)
        monkeypatch.chdir(root)

    def test_fixture_passes(self) -> None:
        # The title is a lowercase Conventional Commit header, which the
        # sentence-case rule would flag if it were not filtered.
        assert pr_draft_io.check_text(FIXTURE.read_text()).problems == []

    def test_findings_map_to_draft_lines(self) -> None:
        # cspell:ignore recieve
        text = FIXTURE.read_text()
        text = replace_once(text, "Running `tool stamp`", "Running tool recieve")
        text = replace_once(text, "## Why\n\n", "## Why\n")
        found = pr_draft_io.check_text(text).problems
        assert any(line == 15 and "recieve" in why for line, why in found)
        assert any(line == 14 and "rumdl" in why for line, why in found)


def test_cog_runs_in_the_worktree_top_from_a_subdirectory(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root / "sub")
    assert pr_prose.title_problems("fix: x") == []
    assert world.calls("cog")[-1]["cwd"] == str(world.root.resolve())


def test_rumdl_line_length_is_off_for_drafts() -> None:
    assert (
        "MD013"
        in (WORKSPACE / ".rumdl.toml").read_text().split("disable")[1].split("]")[0]
    )
