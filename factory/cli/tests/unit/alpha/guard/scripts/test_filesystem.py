# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the script guard with filesystem facts and proposed writes."""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from unit.alpha.guard.scripts import fixtures
from unit.alpha.guard.scripts.fixtures import PLACE, bash, env_of, outside, patch, tool

from tbhbdev.alpha.guard.scripts import filesystem as guard
from tbhbdev.alpha.guard.scripts import writes
from tbhbdev.alpha.guard.scripts.filesystem import first_refusal
from tbhbdev.alpha.guard.scripts.paths import TMP_ROOT, normalize

if TYPE_CHECKING:
    from collections.abc import Callable

home = fixtures.home
libexec = fixtures.libexec
isolate_repository_markers = pytest.fixture(autouse=True)(
    fixtures.isolate_repository_markers
)


type Json = dict[str, object]


SHEBANG = "#!/bin/sh\n"


NEW = "2026-11-09-coord-foo.sh"


def test_the_messages_are_exact(home: Path, libexec: Path) -> None:
    old = libexec / NEW
    old.write_text(SHEBANG)
    env = env_of(home)
    assert first_refusal(tool(home, "Write", file_path=str(old)), env) == (
        f"{old} already exists, and a handed-over script is never edited. "
        "Write the revision to a new file with a .vN suffix, such as "
        "YYYY-MM-DD-<owner>-<slug>.v2.sh or .v2.py."
    )
    bad = libexec / "run.sh"
    assert first_refusal(tool(home, "Write", file_path=str(bad)), env) == (
        f"{bad} isn't a valid name for a script. Name it "
        "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh or .py, with a real "
        "date and no subdirectory. A revision is a new file with a .vN suffix."
    )
    drafts = home / "drafts-x"
    (drafts / ".git").mkdir(parents=True)
    path = drafts / "a.sh"
    assert first_refusal(tool(home, "Write", file_path=str(path)), env) == (
        f"A drafts repository doesn't hold scripts: {path}. "
        "Write scripts for the operator to "
        "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh or .py. "
        "A revision is a new file with a .vN suffix, "
        "because a handed-over script is never edited. "
        "Run logs go in ~/.local/state/tbhb/<same name>/."
    )
    cache = home / ".cache" / "a.sh"
    assert first_refusal(tool(home, "Write", file_path=str(cache)), env) == (
        f"Scripts for the operator don't go in {cache}. "
        "Write scripts for the operator to "
        "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh or .py. "
        "A revision is a new file with a .vN suffix, "
        "because a handed-over script is never edited. "
        "Run logs go in ~/.local/state/tbhb/<same name>/."
    )


@pytest.mark.parametrize("key", ["content", "CodeContent", "Content"])
def test_write_content_fields(home: Path, key: str) -> None:
    data = tool(home, "write_file", Path=str(home / "x"), **{key: SHEBANG})
    assert first_refusal(data, env_of(home)) == outside(home / "x")


def test_a_numbered_target_is_a_file(home: Path) -> None:
    found = first_refusal(bash(home, "echo x > 2.sh"), env_of(home))
    assert found == outside(home / "work" / "2.sh")
    word = writes.RedirectScanner("echo a2>f").run()
    assert word == [["echo", "a2", ">", "f"]]
    assert writes.redirects(word[0]) == (["f"], ["echo", "a2"])


def test_a_code_repository_work_tree_is_allowed(home: Path) -> None:
    repo = home / "code" / "tools"
    (repo / ".git").mkdir(parents=True)
    path = repo / "bin" / "run.sh"
    data = tool(home, "Write", file_path=str(path))
    assert first_refusal(data, env_of(home)) is None
    assert first_refusal(bash(home, f"cp t.sh {path}"), env_of(home)) is None


def test_a_linked_work_tree_of_a_code_repository_is_allowed(home: Path) -> None:
    main = home / "code" / "tools"
    link = main / ".worktrees" / "wt"
    git_dir = main / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n")
    link.mkdir(parents=True)
    (link / ".git").write_text(f"gitdir: {git_dir}\n")
    data = tool(home, "Write", file_path=str(link / "run.sh"))
    assert first_refusal(data, env_of(home)) is None


@pytest.mark.parametrize("prefix", [TMP_ROOT, "/private/tmp"])
def test_this_sessions_scratchpad_is_allowed(home: Path, prefix: str) -> None:
    path = f"{prefix}/claude-501/-Users-x-proj/0a1b-2c/scratchpad/run.sh"
    data = tool(home, "Write", file_path=path)
    assert first_refusal(data, env_of(home)) is None
    deeper = f"{prefix}/claude-501/-Users-x-proj/0a1b-2c/scratchpad/a/b.sh"
    assert first_refusal(tool(home, "Write", file_path=deeper), env_of(home)) is None


@pytest.mark.parametrize(
    "path",
    [
        f"{TMP_ROOT}/claude-x/p/s/scratchpad/run.sh",
        f"{TMP_ROOT}/claude-/p/s/scratchpad/run.sh",
        f"{TMP_ROOT}/claude-501/p/scratchpad/run.sh",
        f"{TMP_ROOT}/claude-501/p/s/t/scratchpad/run.sh",
        f"{TMP_ROOT}/claude-501/p/s/other/run.sh",
        f"{TMP_ROOT}/claude-501/p/s/scratchpad.sh",
        f"{TMP_ROOT}/claude-501//s/scratchpad/run.sh",
        f"{TMP_ROOT}/x/claude-501/p/s/scratchpad/run.sh",
        f"{TMP_ROOT}/claude-501/p/s/scratchpad/../run.sh",
        f"{TMP_ROOT}/claude-501/../a.sh",
    ],
)
def test_other_paths_in_tmp_are_refused(home: Path, path: str) -> None:
    data = tool(home, "Write", file_path=path)
    expected = normalize(path)
    found = first_refusal(data, env_of(home))
    assert found == outside(expected)


def test_a_run_scoped_tmpdir_is_allowed(home: Path) -> None:
    data = tool(home, "Write", file_path=f"{TMP_ROOT}/run-1/x/a.sh")
    assert first_refusal(data, env_of(home, f"{TMP_ROOT}/run-1")) is None
    assert first_refusal(data, env_of(home, "/private/tmp/run-1/")) is None
    assert first_refusal(data, env_of(home, f"{TMP_ROOT}/run-1/x")) is None
    data = tool(home, "Write", file_path="/private/tmp/run-1/a.sh")
    assert first_refusal(data, env_of(home, f"{TMP_ROOT}/run-1")) is None


@pytest.mark.parametrize(
    "tmpdir",
    [
        None,
        "",
        TMP_ROOT,
        f"{TMP_ROOT}/",
        "/private/tmp",
        f"{TMP_ROOT}/run-2",
        f"{TMP_ROOT}/run-1/x/y",
        f"{TMP_ROOT}/run-10",
        "run-1",
        "/var/folders/T",
    ],
)
def test_a_tmpdir_that_is_not_run_scoped_does_not_allow(
    home: Path, tmpdir: str | None
) -> None:
    data = tool(home, "Write", file_path=f"{TMP_ROOT}/run-1/a.sh")
    found = first_refusal(data, env_of(home, tmpdir))
    assert found == outside(f"{TMP_ROOT}/run-1/a.sh")


def test_a_tmpdir_with_parent_references_is_normalized(home: Path) -> None:
    data = tool(home, "Write", file_path=f"{TMP_ROOT}/a.sh")
    assert first_refusal(data, env_of(home, f"{TMP_ROOT}/x/..")) == outside(
        f"{TMP_ROOT}/a.sh"
    )
    assert first_refusal(data, env_of(home, f"{TMP_ROOT}/../tmp/x/..")) == outside(
        f"{TMP_ROOT}/a.sh"
    )


def test_a_tmpdir_outside_tmp_allows_nothing_there(home: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    data = tool(home, "Write", file_path=str(run / "a.sh"))
    assert first_refusal(data, env_of(home, str(run))) is None


def test_a_drafts_work_tree_is_refused(home: Path) -> None:
    (home / "drafts-private" / ".git").mkdir(parents=True)
    path = home / "drafts-private" / "drafts" / "a.sh"
    data = tool(home, "Write", file_path=str(path))
    found = first_refusal(data, env_of(home))
    assert found == f"A drafts repository doesn't hold scripts: {path}. {PLACE}"
    other = tool(home, "Write", file_path=str(path.with_suffix(".md")))
    assert first_refusal(other, env_of(home)) is None


def test_the_name_must_start_with_drafts(home: Path) -> None:
    for name in ["my-drafts", "draft", "a-drafts"]:
        (home / name / ".git").mkdir(parents=True)
        data = tool(home, "Write", file_path=str(home / name / "a.sh"))
        assert first_refusal(data, env_of(home)) is None, name
    (home / "drafts").mkdir()
    (home / "drafts" / ".git").mkdir()
    data = tool(home, "Write", file_path=str(home / "drafts" / "a.sh"))
    assert first_refusal(data, env_of(home)) is not None


def make_linked(home: Path, main: str, *, relative: bool, bare: bool = False) -> Path:
    base = home / main
    git_dir = base / ("" if bare else ".git") / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n")
    link = home / "elsewhere" / "wt"
    link.mkdir(parents=True)
    target = os.path.relpath(git_dir, link) if relative else str(git_dir)
    (link / ".git").write_text(f"gitdir: {target}\n")
    return link


@pytest.mark.parametrize("relative", [False, True])
def test_a_linked_work_tree_of_a_drafts_repository_is_refused(
    home: Path, *, relative: bool
) -> None:
    link = make_linked(home, "drafts-private", relative=relative)
    data = tool(home, "Write", file_path=str(link / "sub" / "a.sh"))
    found = first_refusal(data, env_of(home))
    assert (
        found == f"A drafts repository doesn't hold scripts: {link}/sub/a.sh. {PLACE}"
    )


def test_a_bare_drafts_repository_names_its_linked_work_trees(home: Path) -> None:
    link = make_linked(home, "drafts.git", relative=False, bare=True)
    data = tool(home, "Write", file_path=str(link / "a.sh"))
    assert first_refusal(data, env_of(home)) is not None


def test_a_linked_work_tree_of_another_repository_is_allowed(home: Path) -> None:
    link = make_linked(home, "tools", relative=False)
    data = tool(home, "Write", file_path=str(link / "a.sh"))
    assert first_refusal(data, env_of(home)) is None


def test_a_drafts_named_linked_work_tree_is_refused_by_its_own_name(home: Path) -> None:
    link = make_linked(home, "tools", relative=False)
    drafty = link.with_name("drafts-wt")
    link.rename(drafty)
    data = tool(home, "Write", file_path=str(drafty / "a.sh"))
    assert first_refusal(data, env_of(home)) is not None


def test_main_repo_names(home: Path) -> None:
    top = home / "wt"
    top.mkdir()
    dot_git = top / ".git"
    dot_git.write_text("gitdir: nowhere\n")
    assert guard.main_repo_names(top, dot_git) == []
    git_dir = home / "main" / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    dot_git.write_text(f"gitdir: {git_dir}\n")
    assert guard.main_repo_names(top, dot_git) == []
    (git_dir / "commondir").write_text("../..\n")
    assert guard.main_repo_names(top, dot_git) == ["main"]
    (git_dir / "commondir").write_text(f"{home}/other.git")
    assert guard.main_repo_names(top, dot_git) == ["other.git"]
    dot_git.write_bytes(b"\xff")
    assert guard.main_repo_names(top, dot_git) == []


@pytest.fixture
def case_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Path lookups match names exactly, as on a case-sensitive filesystem."""

    def exact(method: Callable[[Path], object], missing: object) -> object:
        def lookup(self: Path) -> object:
            if self.name not in {entry.name for entry in self.parent.iterdir()}:
                if isinstance(missing, Exception):
                    raise missing
                return missing
            return method(self)

        return lookup

    for name, missing in (
        ("is_dir", False),
        ("is_file", False),
        ("read_text", FileNotFoundError()),
    ):
        monkeypatch.setattr(Path, name, exact(getattr(Path, name), missing))


@pytest.mark.usefixtures("case_sensitive")
def test_names_match_the_git_entries_exactly(home: Path) -> None:
    upper = home / "upper"
    (upper / ".GIT").mkdir(parents=True)
    assert guard.work_tree_names(str(upper / "a.sh")) is None
    top = home / "wt"
    top.mkdir()
    dot_git = top / ".git"
    git_dir = home / "main" / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    dot_git.write_text(f"gitdir: {git_dir}\n")
    (git_dir / "COMMONDIR").write_text("../..\n")
    assert guard.main_repo_names(top, dot_git) == []


def test_work_tree_names(home: Path) -> None:
    repo = home / "r"
    (repo / ".git").mkdir(parents=True)
    assert guard.work_tree_names(str(repo / "a" / "b" / "c.sh")) == ["r"]
    assert guard.work_tree_names(str(repo / "c.sh")) == ["r"]
    assert guard.work_tree_names(str(home / "nothing" / "c.sh")) is None
    nested = repo / "sub"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: nowhere")
    assert guard.work_tree_names(str(nested / "c.sh")) == ["sub"]
    assert guard.work_tree_names("/c.sh") is None


@pytest.mark.parametrize(
    "name",
    [
        NEW,
        "2026-11-09-coord-foo.v2.sh",
        "2026-11-09-coord-foo.v10.sh",
        "2024-02-29-a-b.sh",
        "2026-11-09-co-ord-foo-bar.sh",
        "2026-1-09-coord-foo.sh",
    ][:-1],
)
def test_a_valid_new_name_is_allowed(home: Path, libexec: Path, name: str) -> None:
    env = env_of(home)
    path = libexec / name
    assert first_refusal(tool(home, "Write", file_path=str(path)), env) is None
    assert first_refusal(bash(home, f"echo x > {path}"), env) is None
    assert first_refusal(bash(home, f"cp t.sh {path}"), env) is None


@pytest.mark.parametrize(
    "name",
    [
        "run.sh",
        "coord-foo.sh",
        "2026-11-09-coord.sh",
        "2026-11-09-coord-.sh",
        "2026-11-09--foo.sh",
        "2026-02-30-coord-foo.sh",
        "2025-02-29-coord-foo.sh",
        "2026-13-01-coord-foo.sh",
        "2026-00-01-coord-foo.sh",
        "2026-01-00-coord-foo.sh",
        "0000-01-01-coord-foo.sh",
        "2026-11-09-Coord-foo.sh",
        "2026-11-09-coord-Foo.sh",
        "2026-11-09-coord_foo.sh",
        "2026-11-09-coord-foo.sh.bak",
        "2026-11-09-coord-foo.bak",
        "2026-11-09-coord-foo",
        "2026-11-09-coord-foo.v.sh",
        "2026-11-09-coord-foo.vx.sh",
        "2026-11-09-coord-foo.v2",
        "2026-11-09-coord-foo.v2.sh.bak",
        "2026-11-09-coord-foo.sh~",
        " 2026-11-09-coord-foo.sh",
        "x2026-11-09-coord-foo.sh",
        "2026-11-09-coord-foo.shx",
        "2026-11-09-coord-foo.v2.v3.sh",
        "02026-11-09-coord-foo.sh",
        "2026-011-09-coord-foo.sh",
        "2026-11-009-coord-foo.sh",
        "20261109-coord-foo.sh",
        "\u0662\u0660\u0662\u0666-\u0661\u0661-\u0660\u0669-coord-foo.sh",
        "2026-11-09-caf\u00e9-foo.sh",
        ".2026-11-09-coord-foo.sh",
    ],
)
def test_a_bad_name_is_refused(home: Path, libexec: Path, name: str) -> None:
    env = env_of(home)
    path = libexec / name
    expected = first_refusal(tool(home, "Write", file_path=str(path)), env)
    assert expected is not None
    assert expected.startswith(f"{path} isn't a valid name for a script.")
    assert first_refusal(bash(home, f"echo '#!/bin/sh' > '{path}'"), env) == expected


def test_a_subdirectory_is_refused(home: Path, libexec: Path) -> None:
    env = env_of(home)
    for path in [libexec / "sub" / NEW, libexec / "a" / "b" / NEW, libexec / NEW / NEW]:
        found = first_refusal(tool(home, "Write", file_path=str(path)), env)
        assert found is not None
        assert found.startswith(f"{path} isn't a valid name")
    (libexec / "sub").mkdir()
    found = first_refusal(bash(home, f"cp t.sh {libexec}/sub/"), env)
    assert found is not None
    assert found.startswith(f"{libexec}/sub/t.sh isn't a valid name")


def test_the_directory_itself_is_not_a_script_name(home: Path, libexec: Path) -> None:
    found = first_refusal(tool(home, "Write", file_path=str(libexec)), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec} already exists")
    found = first_refusal(bash(home, f"cp t.sh {libexec}"), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/t.sh isn't a valid name")


def test_a_name_in_another_directory_is_not_checked(home: Path, libexec: Path) -> None:
    path = libexec.with_name("tbhb2") / "run.sh"
    found = first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found == outside(path)
    path = libexec.parent / "run.sh"
    found = first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found == outside(path)


def test_a_non_script_still_needs_a_libexec_name(home: Path, libexec: Path) -> None:
    path = libexec / "notes.txt"
    found = first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found is not None
    assert found.startswith(f"{path} isn't a valid name")


OVERWRITES = [
    "echo x > {p}",
    "echo x >> {p}",
    "echo x >| {p}",
    "echo x &> {p}",
    "echo x 2> {p}",
    "echo x | tee {p}",
    "echo x | tee -a {p}",
    "cp t.sh {p}",
    "cp t.sh {d}/",
    "cp t.sh {d}",
    "cp -t {d} t.sh",
    "mv t.sh {p}",
    "install -m755 t.sh {p}",
    "ln -sf t.sh {p}",
    "ln -sf {d}/t.sh {d}",
]


@pytest.mark.parametrize("command", OVERWRITES)
def test_writing_to_an_existing_file_is_refused(
    home: Path, libexec: Path, command: str
) -> None:
    (home / "work" / "t.sh").write_text("")
    path = libexec / NEW if "{p}" in command else libexec / "t.sh"
    path.write_text(SHEBANG)
    found = first_refusal(bash(home, command.format(p=path, d=libexec)), env_of(home))
    if command.endswith("{d}") and "-sf" in command:
        assert found is not None
        return
    assert found is not None
    assert found.startswith(
        f"{path} already exists, and a handed-over script is never edited."
    )


def test_every_tool_is_refused_on_an_existing_file(home: Path, libexec: Path) -> None:
    path = libexec / NEW
    path.write_text(SHEBANG)
    env = env_of(home)
    expected = first_refusal(tool(home, "Write", file_path=str(path)), env)
    assert expected is not None
    assert expected.startswith(f"{path} already exists")
    calls: list[Json] = [
        tool(home, "Edit", file_path=str(path), old_string="a", new_string="b"),
        tool(home, "MultiEdit", file_path=str(path), edits=[]),
        tool(home, "NotebookEdit", notebook_path=str(path)),
        tool(
            home, "apply_patch", command=f"*** Begin Patch\n*** Update File: {path}\n"
        ),
        {"toolCall": {"name": "write_file", "args": {"TargetFile": str(path)}}},
    ]
    for data in calls:
        assert first_refusal(data, env) == expected


def test_a_non_script_overwrite_is_refused_too(home: Path, libexec: Path) -> None:
    path = libexec / "2026-11-09-coord-foo.log"
    path.write_text("x")
    found = first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found is not None
    assert found.startswith(f"{path} already exists")


def test_a_dangling_symlink_counts_as_existing(home: Path, libexec: Path) -> None:
    path = libexec / NEW
    path.symlink_to(home / "nowhere")
    found = first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found is not None
    assert found.startswith(f"{path} already exists")


def test_a_symlinks_own_name_is_judged_not_its_target(
    home: Path, libexec: Path
) -> None:
    (home / ".cache").mkdir()
    link = libexec / NEW
    link.symlink_to(home / ".cache")
    found = first_refusal(
        tool(home, "Write", file_path=str(link / "a.sh")), env_of(home)
    )
    assert found is not None
    assert found.startswith(f"{link}/a.sh isn't a valid name")


def test_chmod_reads_and_ls_always_pass(home: Path, libexec: Path) -> None:
    path = libexec / NEW
    path.write_text(SHEBANG)
    for command in [
        f"chmod +x {path}",
        f"chmod 755 {path} && ls {libexec}",
        f"cat {path}",
        f"bash {path}",
        f"ls -l {libexec}",
        f"cp {path} ~/.cache/",
        f"mkdir -p {libexec}/sub",
        f"touch {path}",
        f"sed -i s/a/b/ {path}",
    ]:
        found = first_refusal(bash(home, command), env_of(home))
        if command == f"cp {path} ~/.cache/":
            assert found == outside(f"{home}/.cache/{NEW}")
        else:
            assert found is None, command


def test_the_migration_mv_passes(home: Path, libexec: Path) -> None:
    cache = home / ".cache" / "coord"
    cache.mkdir(parents=True)
    (cache / "x.sh").write_text(SHEBANG)
    command = f"mv ~/.cache/coord/x.sh ~/.local/libexec/tbhb/{NEW}"
    assert first_refusal(bash(home, command), env_of(home)) is None
    (libexec / NEW).write_text(SHEBANG)
    found = first_refusal(bash(home, command), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/{NEW} already exists")


def test_a_revision_goes_in_a_new_file(home: Path, libexec: Path) -> None:
    (libexec / NEW).write_text(SHEBANG)
    path = libexec / "2026-11-09-coord-foo.v2.sh"
    assert first_refusal(tool(home, "Write", file_path=str(path)), env_of(home)) is None


def test_the_log_directory_name_is_not_a_script_for_a_log(home: Path) -> None:
    logs = home / ".local" / "state" / "tbhb" / NEW
    command = f"echo x > {logs}/run.log"
    assert first_refusal(bash(home, command), env_of(home)) is None
    assert first_refusal(bash(home, f"mkdir -p {logs}"), env_of(home)) is None
    found = first_refusal(bash(home, f"echo x > {logs}"), env_of(home))
    assert found == outside(logs)


def test_a_relative_path_resolves_against_the_payload_cwd(home: Path) -> None:
    cache = home / ".cache"
    data: Json = {
        "tool_name": "Write",
        "tool_input": {"file_path": "x/a.sh"},
        "cwd": str(cache),
    }
    assert first_refusal(data, env_of(home)) == outside(cache / "x" / "a.sh")
    data["cwd"] = "/usr/local"
    assert first_refusal(data, env_of(home)) is None


def test_workspace_paths_stand_in_for_cwd(home: Path) -> None:
    data: Json = {
        "toolCall": {"name": "write_file", "args": {"Path": "a.sh"}},
        "workspacePaths": [str(home / ".cache")],
    }
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_without_a_cwd_the_process_cwd_is_used(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    data: Json = {"tool_name": "Write", "tool_input": {"file_path": ".cache/a.sh"}}
    found = first_refusal(data, env_of(home))
    assert found == outside(Path.cwd() / ".cache" / "a.sh")


def test_home_comes_from_the_environment_given(home: Path) -> None:
    data = tool(home, "Write", file_path="~/x.sh")
    assert first_refusal(data, env_of(home)) == outside(home / "x.sh")
    assert first_refusal(data, env_of(home / "work")) == outside(home / "work" / "x.sh")


@pytest.mark.parametrize("field", ["command", "patch", "input"])
def test_apply_patch_add_file(home: Path, field: str) -> None:
    text = patch("*** Add File: ~/.cache/a.sh", "+echo x")
    data = tool(home, "apply_patch", **{field: text})
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_apply_patch_moves_a_script_into_the_cache(home: Path) -> None:
    text = patch("*** Update File: /usr/local/t.sh", "*** Move to: ~/.cache/t")
    data = tool(home, "apply_patch", command=text)
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "t")


def test_apply_patch_adding_a_shebang_file(home: Path) -> None:
    text = patch("*** Add File: ~/.cache/run", "+#!/bin/sh", "+echo")
    data = tool(home, "apply_patch", command=text)
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "run")
    other = patch("*** Add File: ~/.cache/run", "+echo")
    assert first_refusal(tool(home, "apply_patch", command=other), env_of(home)) is None
    assert first_refusal(tool(home, "apply_patch"), env_of(home)) is None
    assert first_refusal(tool(home, "apply_patch", command=5), env_of(home)) is None


def test_apply_patch_names_the_first_refused_file(home: Path, libexec: Path) -> None:
    text = patch(
        f"*** Add File: {libexec}/{NEW}", "+x", "*** Add File: ~/.cache/a.sh", "+x"
    )
    data = tool(home, "apply_patch", command=text)
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


@pytest.mark.parametrize("name", ["Write", "Edit", "MultiEdit"])
def test_claude_file_tools(home: Path, name: str) -> None:
    data = tool(home, name, file_path="~/.cache/a.sh")
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")
    data = tool(home, name, file_path="~/.cache/a.txt")
    assert first_refusal(data, env_of(home)) is None


def test_claude_notebook_edit(home: Path) -> None:
    data = tool(home, "NotebookEdit", notebook_path="~/.cache/a.sh")
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")
    data = tool(home, "NotebookEdit", notebook_path="~/.cache/a.ipynb")
    assert first_refusal(data, env_of(home)) is None
    data = tool(home, "NotebookEdit", notebook_path=3, file_path="~/.cache/a.sh")
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


@pytest.mark.parametrize(
    "key",
    [
        "Path",
        "TargetFile",
        "AbsolutePath",
        "target_file",
        "file_path",
        "filePath",
        "path",
    ],
)
def test_agy_write_file_path_keys(home: Path, key: str) -> None:
    for args in ({key: "~/.cache/a.sh"}, json.dumps({key: "~/.cache/a.sh"})):
        data: Json = {
            "toolCall": {"name": "write_file", "args": args},
            "cwd": str(home),
        }
        found = first_refusal(data, env_of(home))
        assert found == outside(home / ".cache" / "a.sh")


def test_agy_arguments_that_are_not_json_or_an_object(home: Path) -> None:
    bad: list[object] = ["not json", "[]", "5", None, [], 7, ""]
    for args in bad:
        data: Json = {"toolCall": {"name": "write_file", "args": args}}
        assert first_refusal(data, env_of(home)) is None
    data: Json = {
        "toolName": "write_file",
        "toolInput": {"Path": "~/.cache/a.sh"},
        "toolCall": {},
    }
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_agy_run_command(home: Path) -> None:
    data: Json = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "echo x > ~/.cache/a.sh"},
        }
    }
    assert first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_a_payload_with_neither_a_command_nor_a_path_is_allowed(home: Path) -> None:
    cases: list[Json] = [
        {},
        {"tool_input": {}},
        {"tool_input": "x"},
        {"tool_name": "Read", "tool_input": {"file_path": "~/.cache/a.sh", "limit": 5}},
        {"toolCall": 5},
        {"tool_input": {"command": 5}},
        {"tool_input": {"file_path": ""}},
        {"tool_input": {"file_path": 5}},
    ]
    for data in cases:
        assert first_refusal(data, env_of(home)) is None, data


def test_first_refusal_returns_the_first_of_several(home: Path, libexec: Path) -> None:
    command = f"echo x > ~/.cache/a.sh && echo x > {libexec}/bad && echo > /tmp/b.sh"
    assert first_refusal(bash(home, command), env_of(home)) == outside(
        home / ".cache" / "a.sh"
    )
    command = f"echo x > ~/work/ok.txt && echo > {libexec}/run.sh && echo > /tmp/b.sh"
    found = first_refusal(bash(home, command), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/run.sh isn't")
    command = "echo > /usr/ok.sh && echo > /tmp/b.sh"
    assert first_refusal(bash(home, command), env_of(home)) == outside(
        f"{TMP_ROOT}/b.sh"
    )


def test_a_double_slash_does_not_skip_the_tmp_check(home: Path) -> None:
    for command in ("echo x > //tmp/a.sh", "echo x > ///tmp/a.sh"):
        found = first_refusal(bash(home, command), env_of(home))
        assert found == outside(f"{TMP_ROOT}/a.sh")
    found = first_refusal(bash(home, "echo x > //private/tmp/a.sh"), env_of(home))
    assert found == outside(f"{TMP_ROOT}/a.sh")


def test_a_double_slash_does_not_skip_the_home_check(home: Path) -> None:
    found = first_refusal(bash(home, f"echo x > /{home}/.cache/a.sh"), env_of(home))
    assert found == outside(f"{home}/.cache/a.sh")


def test_a_double_slash_does_not_skip_the_libexec_check(
    home: Path, libexec: Path
) -> None:
    (libexec / NEW).write_text(SHEBANG)
    found = first_refusal(bash(home, f"echo x > /{libexec}/{NEW}"), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/{NEW} already exists")
    found = first_refusal(bash(home, f"echo x > /{libexec}/run.sh"), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/run.sh isn't a valid name")


@pytest.fixture
def repo(home: Path) -> Path:
    path = home / "Code" / "repo"
    (path / ".git").mkdir(parents=True)
    (path / "sub").mkdir()
    return path


def test_a_cd_then_a_heredoc_script_is_judged_where_it_lands(
    home: Path, repo: Path
) -> None:
    body = "a.sh <<'E'\n#!/bin/sh\nE\n"
    found = first_refusal(bash(home, f"cd ~/.cache && cat > {body}"), env_of(home))
    assert found == outside(f"{home}/.cache/a.sh")
    assert first_refusal(bash(home, f"cd {repo} && cat > {body}"), env_of(home)) is None
    plain = "cd ~/.cache && cat > a <<'E'\n#!/bin/sh\nE\n"
    assert first_refusal(bash(home, plain), env_of(home)) == outside(f"{home}/.cache/a")


PYTHON_FIRST_LINE = "#!/usr/bin/env -S python3 -I"


PYTHON_NEW = "2026-11-11-coord-foo.py"


@pytest.mark.parametrize("revision", ["", ".v2", ".v10"])
def test_python_names_are_allowed(home: Path, libexec: Path, revision: str) -> None:
    path = libexec / f"2024-02-29-co-ord-foo-bar{revision}.py"
    data = tool(home, "Write", file_path=str(path), content=PYTHON_FIRST_LINE + "\n")
    assert first_refusal(data, env_of(home)) is None


@pytest.mark.parametrize(
    "writer", ["Write", "apply_patch", "heredoc", "tee", "cp", "mv", "install", "ln"]
)
@pytest.mark.parametrize(
    "content",
    [
        PYTHON_FIRST_LINE,
        PYTHON_FIRST_LINE + "\nprint(1)\n",
        "#!/usr/bin/env python3\n",
        "print(1)\n",
        "",
        PYTHON_FIRST_LINE + " \n",
        PYTHON_FIRST_LINE + "x" * 100,
        PYTHON_FIRST_LINE + "\r\n",
        "\n" + PYTHON_FIRST_LINE,
    ],
)
def test_python_first_line(
    home: Path, libexec: Path, writer: str, content: str
) -> None:
    path = libexec / PYTHON_NEW
    if writer == "Write":
        data = tool(home, writer, file_path=str(path), content=content)
    elif writer == "apply_patch":
        lines = ["+" + line for line in content.split("\n")]
        data = tool(home, writer, patch=patch(f"*** Add File: {path}", *lines))
    elif writer in {"heredoc", "tee"}:
        command = f"cat > {path}" if writer == "heredoc" else f"tee {path}"
        data = bash(home, f"{command} <<'E'\n{content}\nE\n")
    else:
        (home / "work" / "source").write_bytes(content.encode())
        data = bash(home, f"{writer} source {path}")
    found = first_refusal(data, env_of(home))
    if content.partition("\n")[0] == PYTHON_FIRST_LINE:
        assert found is None
    else:
        assert found == f"{path} must start with {PYTHON_FIRST_LINE}."


@pytest.mark.parametrize("writer", ["Write", "echo", "cp", "apply_patch"])
def test_unknown_python_content_is_allowed(
    home: Path, libexec: Path, writer: str
) -> None:
    path = libexec / PYTHON_NEW
    if writer == "Write":
        data = tool(home, writer, file_path=str(path))
    elif writer == "apply_patch":
        data = tool(
            home, writer, patch=patch(f"*** Update File: {path}", "@@", "+print(1)")
        )
    else:
        command = f"echo x > {path}" if writer == "echo" else f"cp missing {path}"
        data = bash(home, command)
    assert first_refusal(data, env_of(home)) is None


@pytest.mark.parametrize(
    "name",
    [
        "2026-02-30-coord-foo.py",
        "sub/" + PYTHON_NEW,
        "2026-11-11-coord-foo.vx.py",
        "2026-11-11-Coord-foo.py",
    ],
)
def test_bad_python_names_are_refused(home: Path, libexec: Path, name: str) -> None:
    path = libexec / name
    data = tool(home, "Write", file_path=str(path), content=PYTHON_FIRST_LINE)
    found = first_refusal(data, env_of(home))
    assert found is not None
    assert found.startswith(f"{path} isn't a valid name")


def test_existing_python_file_is_refused(home: Path, libexec: Path) -> None:
    path = libexec / PYTHON_NEW
    path.write_text(PYTHON_FIRST_LINE)
    data = tool(home, "Write", file_path=str(path), content="")
    found = first_refusal(data, env_of(home))
    assert found is not None
    assert found.startswith(f"{path} already exists")


@pytest.mark.parametrize(
    "location", ["home", "tmp", "drafts", "repo", "scratchpad", "tmpdir"]
)
@pytest.mark.parametrize("writer", ["Write", "cp"])
def test_python_scripts_elsewhere(home: Path, location: str, writer: str) -> None:
    dirs = {
        "home": str(home),
        "tmp": TMP_ROOT,
        "drafts": str(home / "drafts-test"),
        "repo": str(home / "repo"),
        "scratchpad": f"{TMP_ROOT}/claude-123/project/session/scratchpad",
        "tmpdir": f"{TMP_ROOT}/guard-python-run",
    }
    for name in ("drafts", "repo"):
        (Path(dirs[name]) / ".git").mkdir(parents=True)
    path = dirs[location] + "/script.py"
    data = (
        tool(home, "Write", file_path=path, content="print(1)")
        if writer == "Write"
        else bash(home, f"cp missing.py {dirs[location]}/renamed")
    )
    found = first_refusal(data, env_of(home, dirs["tmpdir"]))
    if location in {"home", "tmp", "drafts"}:
        assert found is not None
        assert ("drafts repository" if location == "drafts" else "don't go in") in found
    else:
        assert found is None


def test_python_source_read_failure_is_unknown(
    home: Path, libexec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = libexec / PYTHON_NEW
    source = home / "work" / "source.py"
    source.mkdir()

    def is_file(_path: Path) -> bool:
        return True

    monkeypatch.setattr(Path, "is_file", is_file)
    assert first_refusal(bash(home, f"cp source.py {path}"), env_of(home)) is None


def test_python_fifo_source_is_unknown(
    home: Path, libexec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.mkfifo(home / "work" / "source.py")

    def unexpected_open(_path: Path, *_args: object, **_kwargs: object) -> None:
        pytest.fail("a FIFO source must never be opened")

    monkeypatch.setattr(Path, "open", unexpected_open)
    assert (
        first_refusal(bash(home, f"cp source.py {libexec / PYTHON_NEW}"), env_of(home))
        is None
    )


def test_python_patch_first_line_without_end_marker(home: Path, libexec: Path) -> None:
    path = libexec / PYTHON_NEW
    data = tool(
        home, "apply_patch", patch=f"*** Add File: {path}\n+{PYTHON_FIRST_LINE}"
    )
    assert first_refusal(data, env_of(home)) is None


def test_python_source_invalid_bytes_are_refused(home: Path, libexec: Path) -> None:
    source = home / "work" / "source"
    source.write_bytes(PYTHON_FIRST_LINE.encode() + b"\xff\n")
    path = libexec / PYTHON_NEW
    assert (
        first_refusal(bash(home, f"cp source {path}"), env_of(home))
        == f"{path} must start with {PYTHON_FIRST_LINE}."
    )


def test_python_content_fields(home: Path, libexec: Path) -> None:
    path = libexec / PYTHON_NEW
    for key in ("content", "CodeContent", "Content"):
        data = tool(home, "write_file", Path=str(path), **{key: PYTHON_FIRST_LINE})
        assert first_refusal(data, env_of(home)) is None
        data["tool_input"] = {"Path": str(path), key: ""}
        assert (
            first_refusal(data, env_of(home))
            == f"{path} must start with {PYTHON_FIRST_LINE}."
        )
