# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity documents staging."""

from __future__ import annotations

import subprocess
lazy from pathlib import Path

import jsonschema
import yaml
from unit.alpha.identity.identity_examples import Entry, document, entry
from unit.alpha.identity.identity_fixtures import (
    DATA,
    commit,
    loaded,
    run_git,
    stage_doc,
    staged_text,
)
lazy import pytest

from tbhbdev.alpha.identity.documents import frontmatter as documents_frontmatter
from tbhbdev.alpha.identity.documents import staging as documents_staging


def listing(*entries: Entry, indent: int = 2) -> list[str]:
    return [
        line for e in entries for line in documents_frontmatter.render_entry(e, indent)
    ]


def test_git_bytes_returns_raw_stdout_and_feeds_stdin(repo: Path) -> None:
    raw = b"\xff\n\n"
    code, output = documents_staging.git_raw(
        "hash-object", "-w", "--stdin", cwd=repo, data=raw
    )
    assert code == 0
    blob = output.decode().strip()
    assert documents_staging.git_raw("cat-file", "blob", blob, cwd=repo) == (0, raw)
    code, output = documents_staging.git_raw("nonsense-command", cwd=repo)
    assert code != 0
    assert output == b""


def test_staged_docs_picks_stampable_markdown(repo: Path) -> None:
    for rel in (
        "docs/a.md",
        "docs/sp ace/b c.md",
        "docs/d.MD",
        "a/b/c/deep.md",
        "docs/guide/index.md",
        "docs/log.md",
        "docs/AGENTS.md",
        "sub/CLAUDE.md",
        "README.md",
        "docs/notes.txt",
        "docs/data.md.bak",
        "docs/mdfile",
        "tools/testdata/x.md",
        "testdata/y.md",
        "a/testdata/b/z.md",
    ):
        stage_doc(repo, rel, "x\n")
    got = documents_staging.staged_docs(repo)
    assert sorted(d[0] for d in got) == [
        "a/b/c/deep.md",
        "docs/a.md",
        "docs/d.MD",
        "docs/sp ace/b c.md",
    ]
    for rel, mode, blob in got:
        assert mode == "100644"
        assert blob == run_git(repo, "rev-parse", f":{rel}").strip()


def test_staged_docs_modes_and_changes(repo: Path) -> None:
    stage_doc(repo, "docs/run.md", "x\n")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    stage_doc(repo, "docs/keep.md", "k\n")
    stage_doc(repo, "docs/gone.md", "g\n")
    stage_doc(repo, "docs/old.md", "same\nbody\nlines\nhere\n")
    commit(repo)
    stage_doc(repo, "docs/keep.md", "changed\n")
    run_git(repo, "rm", "-q", "docs/gone.md")
    run_git(repo, "mv", "docs/old.md", "docs/new.md")
    got = {rel: mode for rel, mode, _ in documents_staging.staged_docs(repo)}
    assert got == {"docs/keep.md": "100644", "docs/new.md": "100644"}
    stage_doc(repo, "docs/run.md", "x2\n")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    assert {r: m for r, m, _ in documents_staging.staged_docs(repo)}[
        "docs/run.md"
    ] == "100755"


def test_staged_docs_reads_the_repository_it_is_given(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_doc(repo, "docs/a.md", "x\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/a.md"]


def test_staged_docs_skips_files_a_merge_brings_in_unchanged(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_doc(repo, "docs/shared.md", "base\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-c", "side")
    stage_doc(repo, "docs/side.md", "side\n")
    stage_doc(repo, "docs/both.md", "side\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-")
    stage_doc(repo, "docs/main.md", "main\n")
    stage_doc(repo, "docs/shared.md", "main change\n")
    commit(repo)
    run_git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    (repo / "docs" / "side.md").write_text("edited after the merge\n", encoding="utf-8")
    assert documents_staging.staged_docs(repo) == []
    stage_doc(repo, "docs/resolved.md", "by the merge\n")
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/resolved.md"]


def test_staged_docs_keeps_a_file_that_differs_from_both_parents(repo: Path) -> None:
    stage_doc(repo, "docs/a.md", "1\n2\n3\n4\n5\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-c", "side")
    stage_doc(repo, "docs/a.md", "one\n2\n3\n4\n5\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-")
    stage_doc(repo, "docs/a.md", "1\n2\n3\n4\nfive\n")
    commit(repo)
    run_git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/a.md"]


def test_staged_docs_warns_and_keeps_all_when_git_cannot_compare(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage_doc(repo, "docs/a.md", "x\n")
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="ascii")
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/a.md"]
    assert "can't compare with " + "0" * 40 in capsys.readouterr().err


def test_staged_docs_ignores_a_branch_named_merge_head(repo: Path) -> None:
    stage_doc(repo, "docs/a.md", "x\n")
    commit(repo)
    run_git(repo, "branch", "MERGE_HEAD")
    stage_doc(repo, "docs/b.md", "y\n")
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/b.md"]


def test_staged_docs_keeps_a_file_the_merge_moved_and_edited(repo: Path) -> None:
    body = "".join(f"line {n}\n" for n in range(20))
    stage_doc(repo, "docs/old.md", body)
    commit(repo)
    run_git(repo, "switch", "-q", "-c", "side")
    stage_doc(repo, "docs/old.md", body + "tail\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-")
    run_git(repo, "mv", "docs/old.md", "docs/new.md")
    commit(repo)
    run_git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/new.md"]


def test_staged_docs_reads_git_output_as_bytes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Work trees on some file systems can't hold a non-UTF-8 name, so the bytes git
    # would print are faked under the helper: raw bytes unless text mode is asked.
    stage_doc(repo, "docs/a.md", "x\n")
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="ascii")
    raw = (
        b":000000 100644 0000000 1111111 A\0non-doc-\xff.txt\0"
        b":100644 100644 aaaaaaa bbbbbbb M\0docs/a.md\0"
    )
    real = subprocess.run

    def fake_run(
        cmd: list[str],
        *,
        cwd: Path,
        text: bool = False,
        **options: object,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        if cmd[1] != "diff":
            return real(
                cmd,
                cwd=cwd,
                text=True,
                capture_output=bool(options.get("capture_output")),
                check=bool(options.get("check")),
            )
        if text:
            return subprocess.CompletedProcess(cmd, 0, stdout=raw.decode())
        return subprocess.CompletedProcess(cmd, 0, stdout=raw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/a.md"]


def test_staged_docs_after_a_cherry_pick_stamps_the_picked_file(repo: Path) -> None:
    stage_doc(repo, "docs/base.md", "base\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-c", "side")
    stage_doc(repo, "docs/picked.md", "picked\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-")
    run_git(repo, "cherry-pick", "--no-commit", "side")
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/picked.md"]


def test_staged_docs_empty_without_staged_changes(repo: Path) -> None:
    assert documents_staging.staged_docs(repo) == []


def test_stamp_document_rewrites_index_and_worktree_together(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage_doc(repo, "docs/a.md")
    (doc,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(repo, doc, [entry(effort="high")])
    stamped = document("generated:", *listing(entry(effort="high")))
    assert (repo / "docs" / "a.md").read_text(encoding="utf-8") == stamped
    assert staged_text(repo, "docs/a.md") == stamped.encode()
    assert run_git(repo, "status", "--porcelain").startswith("A  docs/a.md")
    assert capsys.readouterr().err == "tbhbdev alpha identity: stamped docs/a.md\n"
    (again,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(repo, again, [entry(effort="high")])
    assert capsys.readouterr().err == ""
    assert staged_text(repo, "docs/a.md") == stamped.encode()


def test_stamp_document_keeps_the_file_mode(repo: Path) -> None:
    stage_doc(repo, "docs/run.md")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    (doc,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(repo, doc, [entry()])
    assert run_git(repo, "ls-files", "-s", "docs/run.md").startswith("100755 ")
    assert "generated:" in staged_text(repo, "docs/run.md").decode()


def test_stamp_document_preserves_unstaged_work_tree_changes(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    path = repo / "docs" / "a.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "unstaged tail\n", encoding="utf-8"
    )
    (doc,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(repo, doc, [entry()])
    stamped = document("generated:", *listing(entry()))
    assert staged_text(repo, "docs/a.md") == stamped.encode()
    assert path.read_text(encoding="utf-8") == stamped + "unstaged tail\n"


def test_stamp_document_keeps_the_work_tree_when_it_cannot_be_stamped(
    repo: Path,
) -> None:
    stage_doc(repo, "docs/a.md")
    path = repo / "docs" / "a.md"
    path.write_text("no frontmatter here\n", encoding="utf-8")
    (doc,) = documents_staging.staged_docs(repo)
    before = path.stat().st_mtime_ns
    documents_staging.stamp_document(repo, doc, [entry()])
    assert path.read_text(encoding="utf-8") == "no frontmatter here\n"
    assert path.stat().st_mtime_ns == before
    assert "generated:" in staged_text(repo, "docs/a.md").decode()


def test_stamp_document_without_a_work_tree_file(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    (repo / "docs" / "a.md").unlink()
    (doc,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(repo, doc, [entry()])
    assert not (repo / "docs" / "a.md").exists()
    assert "generated:" in staged_text(repo, "docs/a.md").decode()


def test_stamp_document_leaves_unstampable_files_alone(repo: Path) -> None:
    stage_doc(repo, "docs/a.md", "plain\n")
    (doc,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(repo, doc, [entry()])
    assert staged_text(repo, "docs/a.md") == b"plain\n"
    assert (repo / "docs" / "a.md").read_bytes() == b"plain\n"


def test_git_side_commands_run_in_the_given_repository_not_the_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_doc(repo, "docs/a.md")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert [d[0] for d in documents_staging.staged_docs(repo)] == ["docs/a.md"]
    (doc,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(repo, doc, [entry()])
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert (repo / "docs" / "a.md").read_text(encoding="utf-8").count("generated:") == 1


def test_stamped_document_agrees_with_the_frontmatter_schema(repo: Path) -> None:
    stage_doc(repo, "docs/a.md", document())
    (doc,) = documents_staging.staged_docs(repo)
    documents_staging.stamp_document(
        repo, doc, [entry(), entry(role="assisted", model="anthropic:t")]
    )
    front = yaml.safe_load(staged_text(repo, "docs/a.md").decode().split("---\n")[1])
    jsonschema.validate(front, loaded(DATA / "frontmatter.schema.json"))
