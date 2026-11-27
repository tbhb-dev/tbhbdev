# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity documents tests."""

import subprocess
import time
from typing import TYPE_CHECKING, cast

from integration.alpha.identity.identity_test_support import (
    CLAUDE_COMMIT_ENV,
    HEAD,
    MAIN,
    ROOT_THREAD,
    SESSION_HASH,
    STAMP_AT,
    commit,
    document,
    edit_by,
    entry,
    front_of,
    listing,
    meta,
    patch_call,
    receipts_of,
    run_git,
    stage_doc,
    staged_text,
    turn,
    write_rollout,
)

from tbhbdev.alpha.identity import documents, sessions, values

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tbhbdev.alpha.identity.frontmatter import Entry


def test_git_bytes_returns_raw_stdout_and_feeds_stdin(repo: Path) -> None:
    raw = b"\xff\n\n"
    code, blob = documents.git_bytes("hash-object", "-w", "--stdin", cwd=repo, data=raw)
    assert code == 0
    assert documents.git_bytes("cat-file", "blob", blob.decode().strip(), cwd=repo) == (
        0,
        raw,
    )
    code, out = documents.git_bytes("nonsense-command", cwd=repo)
    assert code != 0
    assert out == b""


def test_decode_round_trips_any_bytes() -> None:
    raw = b"a\xff\xfe\x80b\n"
    assert documents.decode(raw).encode(errors="surrogateescape") == raw


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
    got = documents.staged_docs(repo)
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
    got = {rel: mode for rel, mode, _ in documents.staged_docs(repo)}
    assert got == {"docs/keep.md": "100644", "docs/new.md": "100644"}
    stage_doc(repo, "docs/run.md", "x2\n")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    assert {r: m for r, m, _ in documents.staged_docs(repo)}["docs/run.md"] == "100755"


def test_staged_docs_reads_the_repository_it_is_given(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_doc(repo, "docs/a.md", "x\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/a.md"]


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
    assert documents.staged_docs(repo) == []
    stage_doc(repo, "docs/resolved.md", "by the merge\n")
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/resolved.md"]


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
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/a.md"]


def test_staged_docs_skips_files_any_octopus_parent_brings_in(repo: Path) -> None:
    stage_doc(repo, "docs/base.md", "base\n")
    commit(repo)
    for name in ("one", "two"):
        run_git(repo, "switch", "-q", "-c", name, "HEAD")
        stage_doc(repo, f"docs/{name}.md", f"{name}\n")
        commit(repo)
        run_git(repo, "switch", "-q", "-")
    stage_doc(repo, "docs/main.md", "main\n")
    commit(repo)
    run_git(repo, "merge", "-q", "--no-commit", "one", "two")
    assert documents.staged_docs(repo) == []
    stage_doc(repo, "docs/resolved.md", "by the merge\n")
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/resolved.md"]


def test_staged_docs_warns_and_keeps_all_when_git_cannot_compare(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage_doc(repo, "docs/a.md", "x\n")
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="ascii")
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/a.md"]
    assert "can't compare with " + "0" * 40 in capsys.readouterr().err


def test_staged_docs_ignores_a_branch_named_merge_head(repo: Path) -> None:
    stage_doc(repo, "docs/a.md", "x\n")
    commit(repo)
    run_git(repo, "branch", "MERGE_HEAD")
    stage_doc(repo, "docs/b.md", "y\n")
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/b.md"]


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
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/new.md"]


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
        capture_output: bool = False,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        assert kwargs.get("input") is None
        if cmd[1] != "diff":
            return real(
                cmd, cwd=cwd, text=True, capture_output=capture_output, check=check
            )
        if text:
            return subprocess.CompletedProcess(cmd, 0, stdout=raw.decode())
        return subprocess.CompletedProcess(cmd, 0, stdout=raw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/a.md"]


def test_staged_docs_after_a_cherry_pick_stamps_the_picked_file(repo: Path) -> None:
    stage_doc(repo, "docs/base.md", "base\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-c", "side")
    stage_doc(repo, "docs/picked.md", "picked\n")
    commit(repo)
    run_git(repo, "switch", "-q", "-")
    run_git(repo, "cherry-pick", "--no-commit", "side")
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/picked.md"]


def test_staged_docs_empty_without_staged_changes(repo: Path) -> None:
    assert documents.staged_docs(repo) == []


def test_stamp_document_rewrites_index_and_worktree_together(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage_doc(repo, "docs/a.md")
    (doc,) = documents.staged_docs(repo)
    documents.stamp_document(repo, doc, [entry(effort="high")])
    stamped = document("generated:", *listing(entry(effort="high")))
    assert (repo / "docs" / "a.md").read_text(encoding="utf-8") == stamped
    assert staged_text(repo, "docs/a.md") == stamped.encode()
    assert run_git(repo, "status", "--porcelain").startswith("A  docs/a.md")
    assert capsys.readouterr().err == "tbhbdev alpha identity: stamped docs/a.md\n"
    (again,) = documents.staged_docs(repo)
    documents.stamp_document(repo, again, [entry(effort="high")])
    assert capsys.readouterr().err == ""
    assert staged_text(repo, "docs/a.md") == stamped.encode()


def test_stamp_document_keeps_the_file_mode(repo: Path) -> None:
    stage_doc(repo, "docs/run.md")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    (doc,) = documents.staged_docs(repo)
    documents.stamp_document(repo, doc, [entry()])
    assert run_git(repo, "ls-files", "-s", "docs/run.md").startswith("100755 ")
    assert "generated:" in staged_text(repo, "docs/run.md").decode()


def test_stamp_document_preserves_unstaged_work_tree_changes(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    path = repo / "docs" / "a.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "unstaged tail\n", encoding="utf-8"
    )
    (doc,) = documents.staged_docs(repo)
    documents.stamp_document(repo, doc, [entry()])
    stamped = document("generated:", *listing(entry()))
    assert staged_text(repo, "docs/a.md") == stamped.encode()
    assert path.read_text(encoding="utf-8") == stamped + "unstaged tail\n"


def test_stamp_document_keeps_the_work_tree_when_it_cannot_be_stamped(
    repo: Path,
) -> None:
    stage_doc(repo, "docs/a.md")
    path = repo / "docs" / "a.md"
    path.write_text("no frontmatter here\n", encoding="utf-8")
    (doc,) = documents.staged_docs(repo)
    before = path.stat().st_mtime_ns
    documents.stamp_document(repo, doc, [entry()])
    assert path.read_text(encoding="utf-8") == "no frontmatter here\n"
    assert path.stat().st_mtime_ns == before
    assert "generated:" in staged_text(repo, "docs/a.md").decode()


def test_stamp_document_without_a_work_tree_file(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    (repo / "docs" / "a.md").unlink()
    (doc,) = documents.staged_docs(repo)
    documents.stamp_document(repo, doc, [entry()])
    assert not (repo / "docs" / "a.md").exists()
    assert "generated:" in staged_text(repo, "docs/a.md").decode()


def test_stamp_document_preserves_arbitrary_bytes(repo: Path) -> None:
    body = b"\xff\xfe\r\nbinary \x00 tail without newline"
    stage_doc(repo, "docs/a.md", b"---\n" + HEAD.encode() + b"---\n" + body)
    (doc,) = documents.staged_docs(repo)
    documents.stamp_document(repo, doc, [entry()])
    stamped = staged_text(repo, "docs/a.md")
    assert stamped.endswith(b"---\n" + body)
    assert (repo / "docs" / "a.md").read_bytes() == stamped


def test_stamp_document_leaves_unstampable_files_alone(repo: Path) -> None:
    stage_doc(repo, "docs/a.md", "plain\n")
    (doc,) = documents.staged_docs(repo)
    documents.stamp_document(repo, doc, [entry()])
    assert staged_text(repo, "docs/a.md") == b"plain\n"
    assert (repo / "docs" / "a.md").read_bytes() == b"plain\n"


def test_cmd_stamp_docs_outside_a_repository(plain: Path) -> None:
    assert documents.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    assert not list(plain.iterdir())


def test_cmd_stamp_docs_stamps_a_human_commit_from_receipts(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    stage_doc(repo, "docs/b.md")
    edit_by(MAIN, "docs/a.md", 1_800_000_000)
    assert documents.cmd_stamp_docs({}) == 0
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert staged_text(repo, "docs/b.md") == document().encode()
    stamped = front_of(staged_text(repo, "docs/a.md").decode())
    assert stamped["generated"] == [
        {
            "role": "generated",
            "harness": "claude-code",
            "model": "anthropic:claude-opus-5-5",
            "effort": "high",
            "surface": "cli",
            "session": SESSION_HASH,
            "at": STAMP_AT,
        }
    ]
    # Receipts are read, not consumed.
    assert len(receipts_of(repo, SESSION_HASH)) == 1


def test_cmd_stamp_docs_leaves_testdata_fixtures_byte_identical(repo: Path) -> None:
    """Regression: a staged fixture under testdata/ got a ``generated`` block."""
    rel = "tests/testdata/alpha/pull_request/passing-draft.md"
    stage_doc(repo, rel)
    before = (repo / rel).read_bytes()
    edit_by(MAIN, rel, 1_800_000_000)
    assert documents.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    assert staged_text(repo, rel) == before
    assert (repo / rel).read_bytes() == before


def test_cmd_stamp_docs_credits_a_shell_edit_to_the_committer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.write_record(
        SESSION_HASH, {"model": "claude-opus-5-5", "version": "2.1.316"}, repo
    )
    stage_doc(repo, "docs/a.md")
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    assert documents.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    (stamped,) = cast(
        "list[Entry]", front_of(staged_text(repo, "docs/a.md").decode())["generated"]
    )
    assert stamped["at"] == STAMP_AT
    assert stamped["harness"] == "claude-code:2.1.316"
    assert stamped["session"] == SESSION_HASH


def test_cmd_stamp_docs_scans_codex_rollouts(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(repo)),
            turn("gpt-5.6-sol", "low"),
            patch_call("docs/a.md"),
        ],
    )
    stage_doc(repo, "docs/a.md")
    env = {"CODEX_HOME": str(home), "CODEX_SESSION_ID": ROOT_THREAD}
    assert documents.cmd_stamp_docs(env) == 0
    (stamped,) = cast(
        "list[Entry]", front_of(staged_text(repo, "docs/a.md").decode())["generated"]
    )
    assert stamped["harness"] == "codex"
    assert stamped["model"] == "openai:gpt-5.6-sol"
    assert stamped["effort"] == "low"
    assert stamped["session"] == values.sha256_64(ROOT_THREAD)


class RunSpy:
    """Records subprocess.run calls and passes them through."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        real = subprocess.run

        def run(command: list[str], **kwargs: object) -> object:
            self.calls.append((command, kwargs))
            return real(command, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr(subprocess, "run", run)


def test_git_bytes_passes_its_options_to_subprocess(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = RunSpy(monkeypatch)
    documents.git_bytes("hash-object", "--stdin", cwd=repo, data=b"x")
    assert spy.calls == [
        (
            ["git", "hash-object", "--stdin"],
            {"capture_output": True, "cwd": repo, "input": b"x", "check": False},
        )
    ]


def test_git_side_commands_run_in_the_given_repository_not_the_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_doc(repo, "docs/a.md")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert [d[0] for d in documents.staged_docs(repo)] == ["docs/a.md"]
    (doc,) = documents.staged_docs(repo)
    documents.stamp_document(repo, doc, [entry()])
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert (repo / "docs" / "a.md").read_text(encoding="utf-8").count("generated:") == 1
