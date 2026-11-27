# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity edits tests."""

import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

from integration.alpha.identity.identity_test_support import (
    CHILD,
    MAIN,
    OTHER,
    ROOT,
    edit_by,
    hook_env,
    make_ident,
    make_receipt,
    receipts_of,
    returning,
    run_git,
)

from tbhbdev.alpha.identity import edits, storage, values

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_rel_to_repo(repo: Path, plain: Path, tmp_path: Path) -> None:
    (repo / "sub").mkdir()
    got = edits.rel_to_repo(str(repo / "sub" / "f.txt"), repo)
    assert got is not None
    assert got[0] == "sub/f.txt"
    assert got[1] == storage.repo_info(repo)
    assert edits.rel_to_repo("sub/f.txt", repo) == got
    deep = edits.rel_to_repo(str(repo / "new" / "deeper" / "g.txt"), repo)
    assert deep is not None
    assert deep[0] == "new/deeper/g.txt"
    assert edits.rel_to_repo(str(plain / "x"), plain) is None
    assert edits.rel_to_repo(str(tmp_path / "outside.txt")) is None
    for bad in (None, "", 5):
        assert edits.rel_to_repo(bad, repo) is None


def test_rel_to_repo_defaults_to_cwd(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    found = edits.rel_to_repo("a/b.txt")
    assert found is not None
    assert found[0] == "a/b.txt"


def test_rel_to_repo_rejects_paths_above_the_top(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = storage.Repo(
        elsewhere, elsewhere / ".git" / "tbhb-alpha", elsewhere / ".git" / "tbhb-alpha"
    )
    monkeypatch.setattr(edits, "repo_info", lambda _cwd=None: fake)
    assert edits.rel_to_repo(str(repo / "f"), repo) is None
    inside = edits.rel_to_repo(str(elsewhere / "f"), repo)
    assert inside is not None
    assert inside[0] == "f"


def test_rel_to_repo_follows_symlinked_directories(repo: Path, tmp_path: Path) -> None:
    (repo / "real").mkdir()
    (tmp_path / "link").symlink_to(repo / "real")
    found = edits.rel_to_repo(str(tmp_path / "link" / "f.txt"), repo)
    assert found is not None
    assert found[0] == "real/f.txt"


def test_blob_of(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    expected = run_git(repo, "hash-object", "f.txt").strip()
    assert edits.blob_of(repo, "f.txt") == expected
    assert edits.blob_of(repo, "missing.txt") is None
    (repo / "d").mkdir()
    assert edits.blob_of(repo, "d") is None
    monkeypatch.setattr(edits, "git", returning((1, "x")))
    assert edits.blob_of(repo, "f.txt") is None


def test_write_edit_stores_a_receipt(repo: Path, plain: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / "f.txt").write_text("hello\n", encoding="utf-8")
    who = make_ident(CHILD, "anthropic:m", ROOT, "low", "cli")
    edits.write_edit(who, str(repo / "sub" / "f.txt"), repo)
    edits.write_edit(who, str(repo / "gone.txt"), repo)
    edits.write_edit(who, str(plain / "elsewhere.txt"), plain)
    edits.write_edit(who, None, repo)
    store = storage.Repo(
        repo, repo / ".git" / "tbhb-alpha", repo / ".git" / "tbhb-alpha"
    )
    receipts = {str(r["path"]): r for _, r in edits.load_edits(store, ROOT)}
    assert sorted(receipts) == ["gone.txt", "sub/f.txt"]
    kept = receipts["sub/f.txt"]
    assert kept["blob"] == run_git(repo, "hash-object", "sub/f.txt").strip()
    assert {k: kept[k] for k in ("root", "subagent", "harness", "model")} == {
        "root": ROOT,
        "subagent": CHILD,
        "harness": "claude-code",
        "model": "anthropic:m",
    }
    assert (kept["effort"], kept["surface"], kept["consumed"]) == ("low", "cli", False)
    assert isinstance(kept["ts"], float)
    assert receipts["gone.txt"]["blob"] is None
    assert values.Ident.from_receipt(kept) == who


def test_load_edits_skips_consumed_and_unreadable(repo: Path) -> None:
    store = storage.Repo(
        repo, repo / ".git" / "tbhb-alpha", repo / ".git" / "tbhb-alpha"
    )
    who = make_ident()
    folder = store.state / "edits" / ("a" * 16)
    storage.write_json(folder / "keep.json", make_receipt("keep", who))
    storage.write_json(
        folder / "done.json", {**make_receipt("done", who), "consumed": True}
    )
    (folder / "junk.json").write_text("{", encoding="utf-8")
    (folder / "empty.json").write_text("{}", encoding="utf-8")
    (folder / "note.txt").write_text("{}", encoding="utf-8")
    got = edits.load_edits(store, ROOT)
    assert [(p.name, r["path"]) for p, r in got] == [("keep.json", "keep")]
    assert edits.load_edits(store, OTHER) == []


def test_blob_of_applies_the_repos_filters(repo: Path, plain: Path) -> None:
    run_git(repo, "config", "core.autocrlf", "true")
    (repo / "f.txt").write_bytes(b"a\r\nb\r\n")
    run_git(repo, "add", "f.txt")
    staged = run_git(repo, "ls-files", "-s", "f.txt").split()[1]
    assert plain.is_dir()
    assert edits.blob_of(repo, "f.txt") == staged


def test_write_edit_resolves_relative_paths_against_the_base(
    repo: Path, plain: Path
) -> None:
    (repo / "r.txt").write_text("r\n", encoding="utf-8")
    edits.write_edit(make_ident(), "r.txt", repo)
    assert [r["path"] for r in receipts_of(repo, ROOT)] == ["r.txt"]
    assert plain.is_dir()


def test_all_edits_spans_roots_and_skips_consumed(repo: Path) -> None:
    edit_by(MAIN, "a.md", 1)
    edit_by(replace(MAIN, root=OTHER), "b.md", 2)
    info = storage.repo_info()
    assert info is not None
    gone = info.state / "edits" / values.hx(ROOT) / "gone.json"
    storage.write_json(gone, {**make_receipt("c.md", MAIN), "consumed": True})
    (info.state / "edits" / values.hx(ROOT) / "junk.json").write_text(
        "{", encoding="utf-8"
    )
    (info.state / "edits" / "stray.json").write_text("{}", encoding="utf-8")
    assert sorted(str(r["path"]) for r in edits.all_edits(info)) == ["a.md", "b.md"]
    assert edits.all_edits(storage.Repo(repo, repo / "nowhere", repo / "nowhere")) == []


def test_blob_of_only_stores_the_object_on_request(repo: Path) -> None:
    (repo / "f.txt").write_text("blob me\n", encoding="utf-8")
    blob = run_git(repo, "hash-object", "f.txt").strip()
    assert edits.blob_of(repo, "f.txt") == blob
    missing = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", blob],
        check=False,
        capture_output=True,
        env=hook_env(),
    )
    assert missing.returncode != 0
    assert edits.blob_of(repo, "f.txt", store=True) == blob
    assert run_git(repo, "cat-file", "blob", blob) == "blob me\n"


def test_write_edit_stores_the_blob_only_when_asked(repo: Path) -> None:
    (repo / "f.txt").write_text("f\n", encoding="utf-8")
    blob = run_git(repo, "hash-object", "f.txt").strip()
    edits.write_edit(make_ident(), "f.txt")
    (got,) = receipts_of(repo, ROOT)
    assert got["blob"] == blob
    missing = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", blob],
        check=False,
        capture_output=True,
        env=hook_env(),
    )
    assert missing.returncode != 0
    edits.write_edit(make_ident(), "f.txt", store=True)
    assert run_git(repo, "cat-file", "blob", blob) == "f\n"
