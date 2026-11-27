# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity codex scan tests."""

import json
from pathlib import Path

import pytest
from integration.alpha.identity.identity_test_support import (
    CHILD_THREAD,
    MAIN,
    ROOT_THREAD,
    START,
    committed_as,
    fork_lines,
    hook_data,
    hook_rollouts,
    init_repo,
    meta,
    patch_call,
    receipts_of,
    run_hook,
    write_rollout,
)

from tbhbdev.alpha.identity import codex_rollout, codex_scan, storage, values


def test_codex_scan_falls_back_to_repo_top_without_cwd(
    repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex"
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    write_rollout(home, ROOT_THREAD, [meta(id=ROOT_THREAD), patch_call("a.txt")])
    store = storage.repo_info(repo)
    assert store is not None
    codex_scan.codex_scan(ROOT_THREAD, store, {"CODEX_HOME": str(home)})
    (got,) = receipts_of(repo, values.sha256_64(ROOT_THREAD))
    assert got["path"] == "a.txt"
    assert got["blob"] is not None


def test_codex_scan_starts_at_zero_and_uses_the_rollout_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = init_repo(tmp_path / "other")
    (other / "o.txt").write_text("o\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home, ROOT_THREAD, [meta(id=ROOT_THREAD, cwd=str(other)), patch_call("o.txt")]
    )
    offsets: list[int] = []
    real = codex_rollout.rollout_patch_edits

    def spy(path: Path, offset: int, skip_lines: int) -> tuple[list[str], int]:
        offsets.append(offset)
        return real(path, offset, skip_lines)

    monkeypatch.setattr(codex_scan, "rollout_patch_edits", spy)
    info = storage.repo_info()
    assert info is not None
    codex_scan.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert offsets == [0]
    root = values.sha256_64(ROOT_THREAD)
    assert [r["path"] for r in receipts_of(other, root)] == ["o.txt"]
    assert receipts_of(repo, root) == []


def test_codex_scan_ignores_patches_replayed_into_a_fork(
    repo: Path, tmp_path: Path
) -> None:
    for name in ("parent.md", "child.md"):
        (repo / name).write_text("x\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, session_id=ROOT_THREAD), patch_call("parent.md")],
    )
    write_rollout(home, CHILD_THREAD, fork_lines(4))
    info = storage.repo_info()
    assert info is not None
    codex_scan.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    by_thread = {
        r["path"]: r["subagent"]
        for r in receipts_of(repo, values.sha256_64(ROOT_THREAD))
    }
    assert by_thread == {"parent.md": None, "child.md": values.sha256_64(CHILD_THREAD)}


@pytest.mark.parametrize("bad", [None, 0, -3, "4", 2.5])
def test_codex_scan_tolerates_odd_fork_markers(
    repo: Path, tmp_path: Path, bad: object
) -> None:
    (repo / "parent.md").write_text("x\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home,
        CHILD_THREAD,
        [
            meta(id=CHILD_THREAD, session_id=ROOT_THREAD, **{START: bad}),
            patch_call("parent.md"),
        ],
    )
    info = storage.repo_info()
    assert info is not None
    codex_scan.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert len(receipts_of(repo, values.sha256_64(ROOT_THREAD))) == 1


@pytest.mark.usefixtures("repo")
def test_refresh_codex_scans_only_for_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, storage.Repo, object]] = []

    def spy(root: str, info: storage.Repo, env: object) -> None:
        calls.append((root, info, env))

    monkeypatch.setattr(codex_scan, "codex_scan", spy)
    info = storage.repo_info()
    assert info is not None
    env = {"CODEX_SESSION_ID": ROOT_THREAD}
    codex_scan.refresh_codex(values.Committer(values.CODEX, MAIN, MAIN), info, env)
    assert calls == [(ROOT_THREAD, info, env)]
    codex_scan.refresh_codex(committed_as(MAIN), info, env)
    codex_scan.refresh_codex(
        values.Committer(values.ANTIGRAVITY, MAIN, MAIN), info, env
    )
    assert len(calls) == 1


def test_codex_scan_credits_a_forks_replayed_patches_to_the_parent_only(
    repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, session_id=ROOT_THREAD), patch_call("parent.md")],
    )
    write_rollout(home, CHILD_THREAD, fork_lines(4))
    info = storage.repo_info()
    assert info is not None
    codex_scan.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    got = sorted(
        (str(r["path"]), str(r["subagent"]))
        for r in receipts_of(repo, values.sha256_64(ROOT_THREAD))
    )
    assert got == [
        ("child.md", values.sha256_64(CHILD_THREAD)),
        ("parent.md", "None"),
    ]


@pytest.mark.parametrize("hooked", ["root", "child"])
def test_codex_scan_takes_nothing_from_a_hooked_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hooked: str
) -> None:
    home = tmp_path / "codex"
    root, child = hook_rollouts(home, repo)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    (repo / "sub.txt").write_text("sub\n", encoding="utf-8")
    # Only one thread has a hook record; the other never fired a hook.
    start, rollout = (
        ("codex-hook-sessionstart.json", root)
        if hooked == "root"
        else ("codex-hook-subagentstart.json", child)
    )
    run_hook(monkeypatch, hook_data(start, repo, rollout))
    for path, name in ((root, "root.txt"), (child, "sub.txt")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(patch_call(name)) + "\n")
    store = storage.repo_info(repo)
    assert store is not None
    checked: list[str] = []
    real = Path.exists

    def spy(self: Path) -> bool:
        checked.append(self.parent.name)
        return real(self)

    monkeypatch.setattr(Path, "exists", spy)
    # The scan finds the hook records from the store, wherever the process is.
    monkeypatch.chdir(tmp_path)
    codex_scan.codex_scan(ROOT_THREAD, store, {"CODEX_HOME": str(home)})
    monkeypatch.undo()
    assert "sessions" in checked
    got = receipts_of(repo, values.sha256_64(ROOT_THREAD))
    # The hooked thread's rollout patch added nothing; the other's was read.
    expect = ("sub.txt", False) if hooked == "root" else ("root.txt", True)
    assert [(str(r["path"]), r["subagent"] is None) for r in got] == [expect]
