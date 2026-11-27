# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity commit claims."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml
from unit.alpha.identity.identity_examples import (
    CHILD,
    OTHER,
    ROOT,
    make_ident,
    make_receipt,
)
from unit.alpha.identity.identity_fixtures import (
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    SUB_E2E,
    claims_of,
    claude_turn,
    claude_write,
    commit,
    edits_dir,
    generated_subagents,
    git_commit,
    loaded,
    msg_file,
    needs_prek,
    observe_git,
    put_receipt,
    returning,
    run_git,
    stage,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import records, storage
from tbhbdev.alpha.identity.commit import claims as commit_claims
from tbhbdev.alpha.identity.commit import hooks as commit_hooks


def test_write_claim_keys_the_receipts_by_the_index_tree(repo: Path) -> None:
    stage(repo, "a.txt")
    info = storage.repo_info()
    assert info is not None
    commit_claims.write_claim(info, ROOT, [Path("/x/b.json"), Path("/y/a.json")])
    tree = run_git(repo, "write-tree").strip()
    assert claims_of(repo) == {tree: {"root": ROOT, "receipts": ["a.json", "b.json"]}}


def test_write_claim_writes_nothing_when_the_index_has_no_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = storage.repo_info()
    assert info is not None
    monkeypatch.setattr(git_repo, "git", returning((128, "")))
    commit_claims.write_claim(info, ROOT, [Path("a.json")])
    assert not (repo / ".git" / "tbhb-alpha").exists()


def test_cmd_trailer_claims_what_it_credits(repo: Path, tmp_path: Path) -> None:
    stage(repo, "a.txt")
    stage(repo, "b.txt")
    blob = run_git(repo, "rev-parse", ":b.txt").strip()
    (repo / "b.txt").write_text("after staging\n", encoding="utf-8")
    later = run_git(repo, "hash-object", "b.txt").strip()
    session = records.sha256_64("session-raw")
    child = make_ident(CHILD, root=session)
    put_receipt(
        repo, "a.json", make_receipt("a.txt", make_ident(root=session)), session
    )
    put_receipt(repo, "b1.json", make_receipt("b.txt", child, blob), session)
    put_receipt(repo, "b2.json", make_receipt("b.txt", child, later), session)
    put_receipt(repo, "c.json", make_receipt("c.txt", child), session)
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    tree = run_git(repo, "write-tree").strip()
    assert claims_of(repo) == {
        tree: {"root": session, "receipts": ["a.json", "b1.json"]}
    }
    # The edit made after staging is not this commit's: no credit from it alone.
    storage.write_json(edits_dir(repo, session) / "b1.json", {})
    other = msg_file(tmp_path, "s\n")
    assert commit_hooks.cmd_trailer(str(other), None, CLAUDE_COMMIT_ENV) == 0
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
    commit_claims.write_claim(info, ROOT, [claimed, edits_dir(repo) / "missing.json"])
    stale = repo / ".git" / "tbhb-alpha" / "claims" / "stale.json"
    storage.write_json(stale, {"root": ROOT, "receipts": ["later.json"]})
    commit(repo)
    assert commit_claims.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (claimed, later, elsewhere)] == [
        True,
        False,
        False,
    ]
    assert not (edits_dir(repo) / "missing.json").exists()
    assert list(claims_of(repo)) == ["stale"]
    # Nothing to claim: a second post-commit changes nothing.
    assert commit_claims.cmd_consume() == 0
    assert loaded(later)["consumed"] is False


def test_cmd_consume_without_a_claim_consumes_nothing(repo: Path) -> None:
    receipt = put_receipt(repo, "x.json", make_receipt("a.txt", make_ident()))
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: LLM harness=claude-code;session={ROOT}\n")
    assert commit_claims.cmd_consume() == 0
    assert loaded(receipt)["consumed"] is False


def test_cmd_consume_outside_a_repository(plain_checkout: Path) -> None:
    assert commit_claims.cmd_consume() == 0
    assert list(plain_checkout.iterdir()) == []


def test_failed_attempt_then_retry_keeps_and_then_consumes_every_receipt(
    repo: Path, tmp_path: Path
) -> None:
    """Regression: a failed commit attempt must not consume or drop receipts."""
    session = records.sha256_64("session-raw")
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
    assert commit_hooks.cmd_trailer(str(first), None, CLAUDE_COMMIT_ENV) == 0
    # commit-msg rejects the attempt: no commit, so no post-commit.
    assert [loaded(p)["consumed"] for p in (root, sub)] == [False, False]
    retry = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(retry), None, CLAUDE_COMMIT_ENV) == 0
    assert retry.read_text(encoding="utf-8") == first.read_text(encoding="utf-8")
    assert retry.read_text(encoding="utf-8").count("Generated-by:") == 2
    commit(repo)
    assert commit_claims.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (root, sub)] == [True, True]


def test_a_receipt_recorded_during_the_commit_is_left_for_the_next(
    repo: Path, tmp_path: Path
) -> None:
    """Regression: post-commit consumed edits recorded after the trailers ran."""
    session = records.sha256_64("session-raw")
    child = make_ident(CHILD, root=session)
    first = put_receipt(repo, "1.json", make_receipt("b.txt", child), session)
    stage(repo, "b.txt")
    assert (
        commit_hooks.cmd_trailer(str(msg_file(tmp_path)), None, CLAUDE_COMMIT_ENV) == 0
    )
    late = put_receipt(repo, "2.json", make_receipt("b.txt", child), session)
    commit(repo)
    assert commit_claims.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (first, late)] == [True, False]
    stage(repo, "b.txt", "second edit\n")
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
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


@needs_prek
@pytest.mark.parametrize("hook", ["prepare-commit-msg", "commit-msg"])
def test_e2e_a_concurrent_edit_during_a_hook_does_not_fail_the_commit(
    wired: Path, hook: str
) -> None:
    """Regression: under prek these stages failed on a concurrent edit.

    prek reported "files were modified by this hook" and stashed unstaged
    edits away while the hook ran.
    """
    (wired / "base.txt").write_text("unstaged\n", encoding="utf-8")
    shim = wired / ".git" / "hooks" / hook
    lines = shim.read_text(encoding="utf-8").splitlines(keepends=True)
    # Another agent's edit lands while the hook runs, and sees the real file.
    lines.insert(1, "cat base.txt > seen.txt; echo concurrent >> base.txt\n")
    shim.write_text("".join(lines), encoding="utf-8")
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert (wired / "seen.txt").read_text(encoding="utf-8") == "unstaged\n"
    base = (wired / "base.txt").read_text(encoding="utf-8")
    assert base == "unstaged\nconcurrent\n"
    assert generated_subagents(wired) == [None]


@needs_prek
def test_e2e_a_formatter_rewrite_keeps_the_subagent_trailer(wired: Path) -> None:
    config = wired / ".pre-commit-config.yaml"
    loaded_config = cast("dict[str, list[object]]", yaml.safe_load(config.read_text()))
    loaded_config["repos"].append(
        {"repo": "builtin", "hooks": [{"id": "trailing-whitespace"}]}
    )
    config.write_text(yaml.safe_dump(loaded_config), encoding="utf-8")
    run_git(wired, "add", ".pre-commit-config.yaml")
    claude_turn(wired)
    claude_write(wired, "b.txt", "sub   \n", agent_id="agent-raw")
    run_git(wired, "add", "b.txt")
    failed = git_commit(wired, "feat: b", CLAUDE_E2E)
    assert failed.returncode != 0
    assert (wired / "b.txt").read_text(encoding="utf-8") == "sub\n"
    run_git(wired, "add", "b.txt")
    done = git_commit(wired, "feat: b", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [SUB_E2E]
    (receipt,) = (wired / ".git" / "tbhb-alpha" / "edits").rglob("*.json")
    assert loaded(receipt)["consumed"] is True


@needs_prek
def test_e2e_an_edit_after_staging_is_credited_to_the_commit_that_has_it(
    wired: Path,
) -> None:
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    claude_write(wired, "a.txt", "a.txt\nsub\n", agent_id="agent-raw")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [None]
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: sub", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [SUB_E2E]


def test_cmd_consume_asks_git_exact_questions(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo)
    calls = observe_git(monkeypatch)
    assert commit_claims.cmd_consume() == 0
    assert calls[-1] == (("rev-parse", "HEAD^{tree}"), repo)
