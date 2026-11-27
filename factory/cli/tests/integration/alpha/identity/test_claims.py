# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity claims tests."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from integration.alpha.identity.identity_test_support import (
    CHILD,
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    OTHER,
    ROOT,
    SUB_E2E,
    claims_of,
    claude_write,
    commit,
    edits_dir,
    generated_subagents,
    git_commit,
    loaded,
    make_ident,
    make_receipt,
    msg_file,
    needs_prek,
    put_receipt,
    returning,
    run_git,
    stage,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import claims, storage, values
from tbhbdev.alpha.identity import commit as identity_commit

if TYPE_CHECKING:
    import pytest


def test_staged_info(repo: Path, plain: Path) -> None:
    assert claims.staged_info(repo) == ([], {})
    stage(repo, "a.txt")
    stage(repo, "dir/b c.txt")
    paths, blobs = claims.staged_info(repo)
    assert sorted(paths) == ["a.txt", "dir/b c.txt"]
    assert blobs["a.txt"] == run_git(repo, "hash-object", "a.txt").strip()
    assert set(blobs) == set(paths)
    assert claims.staged_info(plain) == ([], {})


def test_staged_info_skips_unmerged_entries(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = {
        "diff": (0, "conflict.txt\x00ok.txt\x00"),
        "ls-files": (
            0,
            "100644 abc 1\tconflict.txt\x00100644 def 0\tok.txt\x00bad entry\x00",
        ),
    }

    def fake(*args: str, **_kwargs: Path | None) -> tuple[int, str]:
        return outputs[args[0]]

    monkeypatch.setattr(claims, "git", fake)
    assert claims.staged_info(repo) == (["conflict.txt", "ok.txt"], {"ok.txt": "def"})


def test_unstaged_blobs_covers_only_staged_files_that_differ(repo: Path) -> None:
    stage(repo, "same.txt")
    stage(repo, "changed.txt")
    stage(repo, "gone.txt")
    stage(repo, "other.txt")
    (repo / "changed.txt").write_text("later\n", encoding="utf-8")
    (repo / "other.txt").write_text("later\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    later = run_git(repo, "hash-object", "changed.txt").strip()
    staged = {"same.txt", "changed.txt", "gone.txt"}
    assert claims.unstaged_blobs(repo, staged) == {"changed.txt": later}


def test_committing_keeps_staged_paths_except_still_unstaged_edits() -> None:
    who = make_ident()
    edits = [
        (Path(name), make_receipt(path, who, blob))
        for name, path, blob in (
            ("staged", "a.txt", "b1"),
            ("unstaged", "a.txt", "b2"),
            ("no-blob", "a.txt", None),
            ("other-path", "c.txt", "b2"),
            ("clean-path", "d.txt", "b2"),
            ("not-staged", "e.txt", "b1"),
        )
    ]
    got = claims.committing(
        edits, {"a.txt", "c.txt", "d.txt"}, {"a.txt": "b2", "c.txt": "x"}
    )
    assert [path.name for path, _ in got] == [
        "staged",
        "no-blob",
        "other-path",
        "clean-path",
    ]


def test_write_claim_keys_the_receipts_by_the_index_tree(repo: Path) -> None:
    stage(repo, "a.txt")
    info = storage.repo_info()
    assert info is not None
    claims.write_claim(info, ROOT, [Path("/x/b.json"), Path("/y/a.json")])
    tree = run_git(repo, "write-tree").strip()
    assert claims_of(repo) == {tree: {"root": ROOT, "receipts": ["a.json", "b.json"]}}


def test_write_claim_writes_nothing_when_the_index_has_no_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = storage.repo_info()
    assert info is not None
    monkeypatch.setattr(claims, "git", returning((128, "")))
    claims.write_claim(info, ROOT, [Path("a.json")])
    assert not (repo / ".git" / "tbhb-alpha").exists()


def test_cmd_trailer_claims_what_it_credits(repo: Path, tmp_path: Path) -> None:
    stage(repo, "a.txt")
    stage(repo, "b.txt")
    blob = run_git(repo, "rev-parse", ":b.txt").strip()
    (repo / "b.txt").write_text("after staging\n", encoding="utf-8")
    later = run_git(repo, "hash-object", "b.txt").strip()
    session = values.sha256_64("session-raw")
    child = make_ident(CHILD, root=session)
    put_receipt(
        repo, "a.json", make_receipt("a.txt", make_ident(root=session)), session
    )
    put_receipt(repo, "b1.json", make_receipt("b.txt", child, blob), session)
    put_receipt(repo, "b2.json", make_receipt("b.txt", child, later), session)
    put_receipt(repo, "c.json", make_receipt("c.txt", child), session)
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    tree = run_git(repo, "write-tree").strip()
    assert claims_of(repo) == {
        tree: {"root": session, "receipts": ["a.json", "b1.json"]}
    }
    # The edit made after staging is not this commit's: no credit from it alone.
    storage.write_json(edits_dir(repo, session) / "b1.json", {})
    other = msg_file(tmp_path, "s\n")
    assert identity_commit.cmd_trailer(str(other), None, CLAUDE_COMMIT_ENV) == 0
    assert f"subagent={CHILD}" not in other.read_text(encoding="utf-8")


def test_cmd_consume_consumes_exactly_the_claim_of_the_committed_tree(
    repo: Path,
) -> None:
    who = make_ident()
    claimed = put_receipt(repo, "claimed.json", make_receipt("a.txt", who))
    later = put_receipt(repo, "later.json", make_receipt("a.txt", who))
    elsewhere = put_receipt(repo, "x.json", make_receipt("a.txt", who), OTHER)
    stage(repo, "a.txt")
    info = storage.repo_info()
    assert info is not None
    claims.write_claim(info, ROOT, [claimed, edits_dir(repo) / "missing.json"])
    stale = repo / ".git" / "tbhb-alpha" / "claims" / "stale.json"
    storage.write_json(stale, {"root": ROOT, "receipts": ["later.json"]})
    commit(repo)
    assert claims.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (claimed, later, elsewhere)] == [
        True,
        False,
        False,
    ]
    assert not (edits_dir(repo) / "missing.json").exists()
    assert list(claims_of(repo)) == ["stale"]
    # Nothing to claim: a second post-commit changes nothing.
    assert claims.cmd_consume() == 0
    assert loaded(later)["consumed"] is False


def test_cmd_consume_without_a_claim_consumes_nothing(repo: Path) -> None:
    receipt = put_receipt(repo, "x.json", make_receipt("a.txt", make_ident()))
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: LLM harness=claude-code;session={ROOT}\n")
    assert claims.cmd_consume() == 0
    assert loaded(receipt)["consumed"] is False


def test_cmd_consume_outside_a_repository(plain: Path) -> None:
    assert claims.cmd_consume() == 0
    assert list(plain.iterdir()) == []


def test_failed_attempt_then_retry_keeps_and_then_consumes_every_receipt(
    repo: Path, tmp_path: Path
) -> None:
    """Regression: a failed commit attempt must not consume or drop receipts."""
    session = values.sha256_64("session-raw")
    root = put_receipt(
        repo, "root.json", make_receipt("a.txt", make_ident(root=session)), session
    )
    sub = put_receipt(
        repo,
        "sub.json",
        make_receipt("b.txt", make_ident(CHILD, root=session)),
        session,
    )
    stage(repo, "a.txt")
    stage(repo, "b.txt")
    first = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(first), None, CLAUDE_COMMIT_ENV) == 0
    # commit-msg rejects the attempt: no commit, so no post-commit.
    assert [loaded(p)["consumed"] for p in (root, sub)] == [False, False]
    retry = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(retry), None, CLAUDE_COMMIT_ENV) == 0
    assert retry.read_text(encoding="utf-8") == first.read_text(encoding="utf-8")
    assert retry.read_text(encoding="utf-8").count("Generated-by:") == 2
    commit(repo)
    assert claims.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (root, sub)] == [True, True]


def test_a_receipt_recorded_during_the_commit_is_left_for_the_next(
    repo: Path, tmp_path: Path
) -> None:
    """Regression: post-commit consumed edits recorded after the trailers ran."""
    session = values.sha256_64("session-raw")
    child = make_ident(CHILD, root=session)
    first = put_receipt(repo, "1.json", make_receipt("b.txt", child), session)
    stage(repo, "b.txt")
    assert (
        identity_commit.cmd_trailer(str(msg_file(tmp_path)), None, CLAUDE_COMMIT_ENV)
        == 0
    )
    late = put_receipt(repo, "2.json", make_receipt("b.txt", child), session)
    commit(repo)
    assert claims.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (first, late)] == [True, False]
    stage(repo, "b.txt", "second edit\n")
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    assert f"subagent={CHILD}" in path.read_text(encoding="utf-8")


@needs_prek
def test_e2e_failed_commit_then_retry_keeps_the_subagent_trailer(
    wired: Path,
) -> None:
    claude_write(wired, "a.txt")
    claude_write(wired, "b.txt", agent_id="agent-raw")
    run_git(wired, "add", "a.txt", "b.txt")
    head = run_git(wired, "rev-parse", "HEAD")
    failed = git_commit(
        wired, "feat: ab", CLAUDE_E2E, "--trailer", "Generated-by: robot"
    )
    assert failed.returncode != 0
    assert run_git(wired, "rev-parse", "HEAD") == head
    done = git_commit(wired, "feat: ab", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert sorted(generated_subagents(wired), key=str) == [None, SUB_E2E]
    receipts = list((wired / ".git" / "tbhb-alpha" / "edits").rglob("*.json"))
    assert [loaded(p)["consumed"] for p in receipts] == [True, True]


def test_staged_info_edge_paths(repo: Path) -> None:
    stage(repo, "committed.txt")
    run_git(repo, "commit", "-q", "--no-verify", "-m", "c")
    (repo / "committed.txt").write_text("changed\n", encoding="utf-8")
    stage(repo, "-dash.txt")
    stage(repo, "tab\there.txt")
    paths, blobs = claims.staged_info(repo)
    assert sorted(paths) == ["-dash.txt", "tab\there.txt"]
    assert sorted(blobs) == ["-dash.txt", "tab\there.txt"]


def test_staged_info_defaults_to_the_process_cwd(repo: Path) -> None:
    stage(repo, "top.txt")
    os.chdir(repo)
    paths, blobs = claims.staged_info()
    assert (paths, sorted(blobs)) == (["top.txt"], ["top.txt"])


def test_cmd_consume_asks_git_exact_questions(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo)
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    real = git_repo.git

    def spy(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        calls.append((args, cwd))
        return real(*args, cwd=cwd)

    monkeypatch.setattr(claims, "git", spy)
    assert claims.cmd_consume() == 0
    assert calls[-1] == (("rev-parse", "HEAD^{tree}"), repo)
