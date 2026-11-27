# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request hook."""

import io
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import BASE

from tbhbdev.alpha.pull_request import command as pr_command
from tbhbdev.alpha.pull_request import draft as pr_draft
from tbhbdev.alpha.pull_request import draft_io as pr_draft_io

if TYPE_CHECKING:
    from unit.alpha.pull_request.pr_test_support import World


host_names = pr_test_support.host_names
world = pr_test_support.world


PATCH = "*** Begin Patch\n*** {verb}: {path}\n*** End Patch\n"
DRAFT_REL = ".github/PULL_REQUEST_DRAFT.md"
LINT_PROBLEM = "pull request hook skipped"


def run_hook(monkeypatch: pytest.MonkeyPatch, harness: str, payload: object) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    return pr_command.main(["hook", harness], {})


def hook_payloads(root: Path) -> dict[str, tuple[str, dict[str, object]]]:
    """(harness, payload) pairs that all write the draft."""
    sub = root / "sub"
    absolute = str(root / DRAFT_REL)
    return {
        "claude-relative": (
            "claude",
            {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(root)},
        ),
        "claude-absolute": (
            "claude",
            {"tool_input": {"file_path": absolute}, "cwd": str(sub)},
        ),
        "claude-from-subdir": (
            "claude",
            {"tool_input": {"file_path": f"../{DRAFT_REL}"}, "cwd": str(sub)},
        ),
        "claude-no-cwd": ("claude", {"tool_input": {"file_path": absolute}}),
        "codex-add": (
            "codex",
            {
                "tool_name": "apply_patch",
                "cwd": str(root),
                "tool_input": {
                    "command": PATCH.format(verb="Add File", path=DRAFT_REL)
                },
            },
        ),
        "codex-move": (
            "codex",
            {
                "cwd": str(sub),
                "tool_input": {
                    "command": PATCH.format(verb="Update File", path="a.md")
                    + f"*** Move to: ../{DRAFT_REL}\n"
                },
            },
        ),
        "agy-dict": (
            "antigravity",
            {
                "cwd": str(root),
                "toolCall": {"name": "write_to_file", "args": {"TargetFile": absolute}},
                "error": "",
            },
        ),
        "agy-json": (
            "antigravity",
            {
                "cwd": str(root),
                "toolName": "replace_file_content",
                "toolInput": json.dumps({"AbsolutePath": absolute}),
            },
        ),
    }


HOOK_NAMES = list(hook_payloads(Path("/r")))


@pytest.fixture
def hooked(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root)
    return world


def write_hook_draft(world: World, text: str) -> None:
    (world.root / DRAFT_REL).write_text(text)


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hook_clean_draft_is_silent(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())[name]
    if name == "claude-no-cwd":
        monkeypatch.chdir(hooked.root / "sub")
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert len(hooked.calls("vale")) == 1


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hook_feeds_problems_back_on_stderr(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    write_hook_draft(hooked, "## Why\n\nw\n")
    harness, payload = hook_payloads(hooked.root.resolve())[name]
    if name == "claude-no-cwd":
        monkeypatch.chdir(hooked.root / "sub")
    assert run_hook(monkeypatch, harness, payload) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"{DRAFT_REL} has problems. Fix them and save the file again.\n"
        f"{DRAFT_REL}:1: the first line after the frontmatter must be the H1 title\n"
    )


def test_hook_outside_tbhb_dev_is_silent(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hooked.git("remote", "set-url", "origin", "git@github.com:someone/x.git")
    write_hook_draft(hooked, "broken")
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    assert capsys.readouterr() == ("", "")
    assert hooked.calls("vale") == []


def test_hook_checks_the_draft_of_the_payloads_worktree(
    hooked: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each worktree has its own draft; the hook checks the one it wrote."""
    hooked.git("add", "-A")
    hooked.commit("base")
    tree = tmp_path / "tree"
    hooked.git("worktree", "add", "-q", "-b", "tree", str(tree))
    write_hook_draft(hooked, BASE)
    (tree / DRAFT_REL).write_text("## Why\n\nw\n")
    payload = {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(tree)}
    assert run_hook(monkeypatch, "claude", payload) == 2
    assert capsys.readouterr().err.startswith(f"{DRAFT_REL} has problems.")
    assert Path.cwd() == tree.resolve()
    payload = {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(hooked.root)}
    assert run_hook(monkeypatch, "claude", payload) == 0
    assert capsys.readouterr() == ("", "")


def test_hook_checks_the_payloads_repository_from_anywhere(
    hooked: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The origin check reads the payload's cwd, not the process's."""
    write_hook_draft(hooked, "## Why\n\nw\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    payload = {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(hooked.root)}
    assert run_hook(monkeypatch, "claude", payload) == 2
    assert capsys.readouterr().err.startswith(f"{DRAFT_REL} has problems.")


def test_hook_reports_lint_findings_with_draft_lines(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hooked.fake("cspell", rc=1, stdout=f"{DRAFT_REL}:3:1 - Unknown word (zz)\n")
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 2
    assert f"{DRAFT_REL}:3: cspell: Unknown word (zz)\n" in capsys.readouterr().err


UNRELATED: list[tuple[str, dict[str, object]]] = [
    ("claude", {"tool_input": {"file_path": "README.md"}}),
    ("claude", {"tool_input": {"file_path": "sub/.github/PULL_REQUEST_DRAFT.md"}}),
    ("claude", {"tool_input": {"file_path": ""}}),
    ("claude", {"tool_input": {"file_path": 3}}),
    ("claude", {"tool_input": {"command": "ls"}}),
    ("claude", {}),
    ("codex", {"tool_input": {"command": "ls .github"}}),
    ("codex", {"tool_input": {"command": 7}}),
    (
        "codex",
        {"tool_input": {"command": PATCH.format(verb="Add File", path="a.md")}},
    ),
    (
        "antigravity",
        {"toolCall": {"name": "run_command", "args": {"TargetFile": DRAFT_REL}}},
    ),
    (
        "antigravity",
        {"toolCall": {"name": "write_file", "args": {"TargetFile": "a.md"}}},
    ),
    ("antigravity", {"toolCall": {"name": "write_file", "args": "not json"}}),
    ("antigravity", {}),
    # A Claude-shaped payload to another harness touches nothing.
    ("codex", {"tool_input": {"file_path": DRAFT_REL}}),
    ("antigravity", {"tool_input": {"file_path": DRAFT_REL}}),
]


@pytest.mark.parametrize(("harness", "payload"), UNRELATED)
def test_hook_ignores_other_tool_calls(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: str,
    payload: dict[str, object],
) -> None:
    write_hook_draft(hooked, "broken")
    payload = {"cwd": str(hooked.root.resolve()), **payload}
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    for name in ("vale", "cspell", "rumdl", "cog"):
        assert hooked.calls(name) == []


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", "null"])
def test_hook_ignores_unreadable_payloads(
    hooked: World, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    write_hook_draft(hooked, "broken")
    assert run_hook(monkeypatch, "claude", stdin) == 0
    assert hooked.calls("vale") == []


def test_hook_with_a_missing_draft_file_reports_it(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 2
    assert (
        f"{DRAFT_REL}:1: cannot read the draft: No such file or directory\n"
        in capsys.readouterr().err
    )


def test_hook_fails_open_when_a_tool_is_missing(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (hooked.bin / "vale").unlink()
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{LINT_PROBLEM}: vale not found")


def boom(*_args: object, **_kwargs: object) -> pr_draft.Checked:
    message = "boom"
    raise RuntimeError(message)


def test_hook_fails_open_on_a_crash(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pr_draft_io, "load_checked", boom)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    assert capsys.readouterr().err == f"{LINT_PROBLEM}: boom\n"


def test_hook_rejects_an_unknown_harness(hooked: World) -> None:
    del hooked
    with pytest.raises(SystemExit) as info:
        pr_command.main(["hook", "vim"], {})
    assert info.value.code == 2


def test_hook_ignores_trailers_and_the_branch(
    hooked: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooked.git("switch", "-q", "-c", "topic")
    hooked.commit("one\n\nGenerated-by: LLM nonsense")
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
