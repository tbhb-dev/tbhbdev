# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity commit hooks."""

from __future__ import annotations

import json
import os
import subprocess
from typing import cast
lazy from pathlib import Path

import pytest
import yaml
from unit.alpha.identity.identity_examples import (
    ASSIST_OTHER,
    CHILD,
    EARLY,
    GOOD,
    LAUNCH,
    OPUS,
    OTHER,
    SESSION_HASH,
    SONNET,
    line_of,
    make_ident,
    make_receipt,
    patch_call,
)
from unit.alpha.identity.identity_fixtures import (
    CHILD_THREAD,
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    CLAUDE_HOOK_ENV,
    HUMAN_COMMIT_ENV,
    NO_SESSION_MESSAGE,
    NOREPLY,
    ROOT_THREAD,
    SESSION_RAW,
    claude_turn,
    claude_write,
    commit,
    feed,
    generated_subagents,
    git_commit,
    hook_data,
    hook_env,
    hook_rollouts,
    init_repo,
    last_message,
    meta,
    msg_file,
    needs_prek,
    no_start,
    put_receipt,
    reply,
    returning,
    run_git,
    run_hook,
    seed_claude,
    stage,
    trailers,
    turn,
    write_rollout,
    write_transcript,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import dispatcher, records, sessions, storage
from tbhbdev.alpha.identity import trailers as identity_trailers
from tbhbdev.alpha.identity.claude import ancestry as claude_ancestry
from tbhbdev.alpha.identity.commit import grammar as commit_grammar
from tbhbdev.alpha.identity.commit import hooks as commit_hooks
from tbhbdev.alpha.identity.commit import policy as commit_policy


def test_foreign_root_session_supplies_the_commit_trailer_model(
    repo: Path, plain_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_claude(plain_checkout)
    foreign = init_repo(tmp_path / "foreign")
    run_git(foreign, "remote", "set-url", "origin", "git@github.com:someone/x.git")
    transcript = write_transcript(
        tmp_path / "session.jsonl", reply(OPUS, None), reply(SONNET, None)
    )
    feed(
        monkeypatch,
        {
            "session_id": "session-raw",
            "cwd": str(foreign),
            "transcript_path": str(transcript),
        },
    )
    assert dispatcher.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    monkeypatch.chdir(repo)
    stage(repo, "a.txt")
    path = msg_file(tmp_path, "fix: record foreign session\n")
    assert commit_hooks.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert no_start(
        identity_trailers.llm_trailers(path.read_text(encoding="utf-8"))
    ) == [
        (
            identity_trailers.KEY_GEN,
            (
                f"LLM effort=low;harness=claude-code:2.1.316;"
                f"model=anthropic:{SONNET};session={SESSION_HASH};surface=cli"
            ),
        )
    ]
    assert not (foreign / ".git" / "tbhb-alpha").exists()


@pytest.mark.usefixtures("repo")
def test_cmd_trailer_skips_squashes_and_human_merges(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), "squash", CLAUDE_COMMIT_ENV) == 0
    assert commit_hooks.cmd_trailer(str(path), "merge", {}) == 0
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"
    assert commit_hooks.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert "Generated-by" in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo")
def test_cmd_trailer_stamps_an_agent_merge(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), "merge", CLAUDE_COMMIT_ENV) == 0
    assert "Generated-by: LLM " in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo", "plain_checkout")
def test_cmd_trailer_does_nothing_outside_harness_or_repo(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, {}) == 0
    assert (
        commit_hooks.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    )  # cwd is `plain`
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"


def test_cmd_trailer_scans_codex_rollouts(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_VERSION": "1",
    }
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(repo)),
            turn("gpt-5.6-sol", "high"),
            patch_call("a.txt"),
        ],
    )
    stage(repo, "a.txt")
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, env) == 0
    session = records.sha256_64(ROOT_THREAD)
    assert path.read_text(encoding="utf-8") == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=high;harness=codex:1;"
        f"model=openai:gpt-5.6-sol;session={session}\n"
    )
    assert len(list((repo / ".git" / "tbhb-alpha" / "marks").glob("*.json"))) == 1
    # Claude does not scan rollouts.
    other = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(other), None, CLAUDE_COMMIT_ENV) == 0


def test_check_warns_about_a_missing_start_whatever_the_gap(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_claude(repo)
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    path.write_text(
        path.read_text(encoding="utf-8").replace("Generated-by: ", "Generated-by:"),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert commit_hooks.cmd_check(str(path), CLAUDE_COMMIT_ENV) == 0
    assert capsys.readouterr() == (
        "",
        "tbhbdev alpha identity: warning: a Generated-by line lacks started_at\n",
    )


def test_cmd_trailer_keeps_the_fallback_without_any_generated_by(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\n{ASSIST_OTHER[0]}: {ASSIST_OTHER[1]}\n")
    monkeypatch.setattr(
        claude_ancestry, "ancestry", returning(["git commit --amend -m x"])
    )
    path = msg_file(tmp_path, "x\n")
    assert commit_hooks.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    keys = [
        k for k, _ in identity_trailers.llm_trailers(path.read_text(encoding="utf-8"))
    ]
    assert keys == ["Generated-by", "Assisted-by"]


def test_cmd_trailer_merges_a_model_less_receipt_into_its_twin(
    repo: Path, tmp_path: Path
) -> None:
    stage(repo, "a.txt")
    sessions.write_record(
        SESSION_HASH, {"model": "claude-opus-5-5", "effort": "low"}, repo
    )
    full = make_ident(
        model="anthropic:claude-opus-5-5", root=SESSION_HASH, effort="low"
    )
    bare = make_ident(model=None, root=SESSION_HASH)
    put_receipt(repo, "a1.json", make_receipt("a.txt", full, ts=2), SESSION_HASH)
    put_receipt(repo, "a2.json", make_receipt("a.txt", bare, ts=1), SESSION_HASH)
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    text = path.read_text(encoding="utf-8")
    assert text == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=low;harness=claude-code:2.1.316;"
        f"model=anthropic:claude-opus-5-5;session={SESSION_HASH};surface=cli\n"
    )
    assert commit_grammar.check_message(text) == []


def test_cmd_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = msg_file(tmp_path, f"s\n\nGenerated-by: LLM {GOOD}\n")
    assert commit_hooks.cmd_check(str(good), {}) == 0
    assert capsys.readouterr().err == ""
    bad = msg_file(tmp_path, "s\n\nGenerated-by: nope\nSigned-off-by: A <a@x>\n")
    assert commit_hooks.cmd_check(str(bad), {}) == 1
    assert capsys.readouterr().err == (
        "tbhbdev alpha identity: Generated-by: nope: must start with literal LLM\n"
    )
    signed = msg_file(
        tmp_path, f"s\n\nGenerated-by: LLM {GOOD}\nSigned-off-by: A <a@x>\n"
    )
    assert commit_hooks.cmd_check(str(signed), {}) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("email", ["human@example.invalid", "otheragent@tonyburns.net"])
def test_check_human_without_session_is_unchanged(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    email: str,
) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_EMAIL", email)
    path = msg_file(repo)
    before = path.read_bytes()
    assert commit_hooks.cmd_check(str(path), dict(os.environ)) == 0
    assert capsys.readouterr() == ("", "")
    assert path.read_bytes() == before


def test_check_agent_session_keeps_existing_trailer_rules(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_claude(repo)
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    before = path.read_bytes()
    assert b"Generated-by: LLM" in before
    assert commit_hooks.cmd_check(str(path), CLAUDE_COMMIT_ENV) == 0
    # A new agent commit that lacks started_at stays valid, with a warning.
    assert capsys.readouterr() == (
        "",
        "tbhbdev alpha identity: warning: a Generated-by line lacks started_at\n",
    )
    assert path.read_bytes() == before
    assert commit_hooks.cmd_check(str(path), {}) == 0
    assert capsys.readouterr() == ("", "")
    stamped = before.decode().replace(";surface=", f";started_at={EARLY};surface=")
    assert stamped != before.decode()
    path.write_text(stamped, encoding="utf-8")
    assert commit_hooks.cmd_check(str(path), CLAUDE_COMMIT_ENV) == 0
    assert capsys.readouterr() == ("", "")


@needs_prek
@pytest.mark.parametrize(
    "roles", [("AUTHOR", "COMMITTER"), ("AUTHOR",), ("COMMITTER",)]
)
def test_e2e_agent_identity_requires_session(
    wired: Path, roles: tuple[str, ...]
) -> None:
    stage(wired, "a.txt")
    identities = {
        f"GIT_{role}_{field}": value
        for role in ("AUTHOR", "COMMITTER")
        for field, value in (
            ("NAME", "tbhb.dev Agent" if role in roles else "Human"),
            ("EMAIL", "agent@tonyburns.net" if role in roles else "h@example.invalid"),
        )
    }
    assert not (wired / ".git" / storage.STORE).exists()
    assert not storage.user_store().exists()
    done = git_commit(wired, "feat: a", identities)
    assert done.returncode != 0
    assert NO_SESSION_MESSAGE in done.stderr.splitlines()
    assert run_git(wired, "log", "-1", "--format=%s") == "init\n"


@needs_prek
def test_e2e_an_agent_signoff_fails_the_check(wired: Path) -> None:
    """`git commit -s` by an agent is rejected; the operator signs off."""
    claude_turn(wired)
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E, "-s")
    assert done.returncode != 0
    assert SIGNOFF_BASE in done.stderr


@needs_prek
def test_e2e_an_agent_commit_as_the_agent_identity_passes(wired: Path) -> None:
    claude_turn(wired)
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    who = run_git(wired, "log", "-1", "--format=%an <%ae>%n%cn <%ce>")
    assert who == f"{commit_policy.AGENT_IDENT}\n{commit_policy.AGENT_IDENT}\n"


@needs_prek
@pytest.mark.parametrize(
    ("flags", "extra", "role", "who"),
    [
        ([f"--author={NOREPLY}"], {}, "author", NOREPLY),
        (
            [],
            {"GIT_COMMITTER_EMAIL": "c@example.invalid"},
            "committer",
            "tbhb.dev Agent <c@example.invalid>",
        ),
    ],
    ids=["noreply-author", "wrong-committer"],
)
def test_e2e_an_agent_commit_with_another_identity_fails(
    wired: Path, flags: list[str], extra: dict[str, str], role: str, who: str
) -> None:
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", {**CLAUDE_E2E, **extra}, *flags)
    assert done.returncode != 0
    assert (
        f"tbhbdev alpha identity: {role} is {who!r}; "
        f"an agent commit must use {commit_policy.AGENT_IDENT}"
    ) in done.stderr
    assert run_git(wired, "log", "-1", "--format=%s") == "init\n"


@needs_prek
def test_e2e_a_human_commit_keeps_its_own_identity(wired: Path) -> None:
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", HUMAN_COMMIT_ENV)
    assert done.returncode == 0, done.stderr
    assert run_git(wired, "log", "-1", "--format=%ae") == "h@example.invalid\n"
    assert trailers(wired) == []


def test_ident_problems_accepts_the_agent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_repo, "git", agent_git)
    assert commit_hooks.ident_problems("", {"CLAUDE_CODE_SESSION_ID": "s"}) == []


def test_ident_problems_refuses_an_identity_git_cannot_give(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, ...]] = []

    def failing(*args: str, **_: object) -> tuple[int, str]:
        asked.append(args)
        return 128, ""

    monkeypatch.setattr(git_repo, "git", failing)
    must = f"an agent commit must use {commit_policy.AGENT_IDENT}"
    assert commit_hooks.ident_problems("", {"CODEX_SESSION_ID": "s"}) == [
        f"author is ''; {must}",
        f"committer is ''; {must}",
    ]
    assert asked == [("var", "GIT_AUTHOR_IDENT"), ("var", "GIT_COMMITTER_IDENT")]
    assert commit_hooks.ident_problems("", {}) == []


def agent_git(*_: str, **__: object) -> tuple[int, str]:
    return 0, f"{commit_policy.AGENT_IDENT} 1794020000 -0500"


SIGNOFF_BASE = (
    "agent commits carry no Signed-off-by or Co-authored-by; the operator signs off"
)


@pytest.mark.parametrize(
    "line",
    [
        f"Signed-off-by: {commit_policy.AGENT_IDENT}",
        f"Signed-off-by: {NOREPLY}",
        "Signed-off-by: Human <h@example.invalid>",
        "Co-authored-by: Human <h@example.invalid>",
        "signed-off-by: Human <h@example.invalid>",
        "CO-AUTHORED-BY: Human <h@example.invalid>",
        "Signed-off-by : Human <h@example.invalid>",
    ],
    ids=["agent", "noreply", "human", "co-author", "lowercase", "uppercase", "space"],
)
def test_ident_problems_refuses_a_signoff_or_co_author(
    monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(git_repo, "git", agent_git)
    text = f"feat: a\n\nbody\n\n{line}\n"
    assert commit_hooks.ident_problems(text, {"CODEX_SESSION_ID": "s"}) == [
        SIGNOFF_BASE
    ]
    assert commit_hooks.ident_problems(text, {}) == [commit_policy.SESSION_ERROR]


def test_ident_problems_reports_the_trailers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_repo, "git", agent_git)
    text = "feat: a\n\nSigned-off-by: A <a@x>\nCo-authored-by: B <b@x>\n"
    assert commit_hooks.ident_problems(text, {"CODEX_SESSION_ID": "s"}) == [
        SIGNOFF_BASE
    ]


@pytest.mark.parametrize(
    "text",
    [
        "feat: a\n",
        "feat: a\n\nGenerated-by: LLM x\n",
        f"feat: a\n\nSee Signed-off-by: {NOREPLY}\n",
    ],
    ids=["none", "other-trailer", "mid-line"],
)
def test_ident_problems_accepts_a_message_without_the_trailers(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr(git_repo, "git", agent_git)
    assert commit_hooks.ident_problems(text, {"CODEX_SESSION_ID": "s"}) == []


def test_ident_problems_ignores_trailers_outside_a_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_repo, "git", agent_git)
    text = (
        f"feat: a\n\nGenerated-by: LLM {GOOD}\n"
        "Signed-off-by: A <a@x>\nCo-authored-by: B <b@x>\n"
    )
    assert commit_hooks.ident_problems(text, {}) == []


def test_cmd_check_reads_the_signoff_from_the_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(git_repo, "git", agent_git)
    msg = tmp_path / "msg"
    msg.write_text(f"feat: a\n\nSigned-off-by: {NOREPLY}\n")
    assert commit_hooks.cmd_check(str(msg), {"CODEX_SESSION_ID": "s"}) == 1
    assert capsys.readouterr().err == f"tbhbdev alpha identity: {SIGNOFF_BASE}\n"


CODEX_E2E = {"CODEX_SESSION_ID": ROOT_THREAD, "CODEX_VERSION": "0.166.0"}


@needs_prek
def test_e2e_a_codex_commit_with_a_noreply_signoff_fails(wired: Path) -> None:
    stage(wired, "a.txt")
    done = git_commit(wired, f"feat: a\n\nSigned-off-by: {NOREPLY}", CODEX_E2E)
    assert done.returncode != 0
    assert f"tbhbdev alpha identity: {SIGNOFF_BASE}" in done.stderr
    assert run_git(wired, "log", "-1", "--format=%s") == "init\n"


@needs_prek
def test_e2e_a_codex_commit_as_the_agent_passes(wired: Path) -> None:
    home = wired.parent / "codex"
    write_rollout(home, ROOT_THREAD, [meta(id=ROOT_THREAD), turn("gpt-5.6-sol", None)])
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", {**CODEX_E2E, "CODEX_HOME": str(home)})
    assert done.returncode == 0, done.stderr
    who = run_git(wired, "log", "-1", "--format=%an <%ae>%n%cn <%ce>")
    assert who == f"{commit_policy.AGENT_IDENT}\n{commit_policy.AGENT_IDENT}\n"
    assert "Signed-off-by" not in last_message(wired)


COG_LIKE = """#!/bin/sh
echo ran >> cog.log
grep -qE '^(feat|fix|chore): ' "$1" && exit 0
echo "cog: not a conventional commit" >&2
exit 1
"""


@pytest.fixture
def with_msg_linter(wired: Path) -> Path:
    """``wired`` plus a prek commit-msg hook (a cog-like linter), reinstalled."""
    (wired / "cog.sh").write_text(COG_LIKE, encoding="utf-8")
    config = wired / ".pre-commit-config.yaml"
    loaded_config = cast("dict[str, list[object]]", yaml.safe_load(config.read_text()))
    loaded_config["repos"].append(
        {
            "repo": "local",
            "hooks": [
                {
                    "id": "cog",
                    "name": "cog verify",
                    "language": "system",
                    "entry": "sh cog.sh",
                    "stages": ["commit-msg"],
                }
            ],
        }
    )
    config.write_text(yaml.safe_dump(loaded_config), encoding="utf-8")
    subprocess.run(
        ["tbhbdev", "alpha", "install", "--harness", "claude"],
        check=True,
        capture_output=True,
        env=hook_env(),
    )
    run_git(wired, "add", "cog.sh", ".pre-commit-config.yaml")
    done = git_commit(wired, "chore: linter", HUMAN_COMMIT_ENV)
    assert done.returncode == 0, done.stderr
    (wired / "cog.log").unlink(missing_ok=True)
    return wired


@needs_prek
def test_e2e_prek_commit_msg_hook_still_rejects_a_message(
    with_msg_linter: Path,
) -> None:
    """Regression: the plain commit-msg hook stopped running prek's hooks."""
    repo = with_msg_linter
    assert (repo / ".git" / "hooks" / "commit-msg.prek").is_file()
    claude_write(repo, "a.txt")
    run_git(repo, "add", "a.txt")
    head = run_git(repo, "rev-parse", "HEAD")
    done = git_commit(repo, "not conventional", CLAUDE_E2E)
    assert done.returncode != 0
    assert "cog: not a conventional commit" in done.stdout + done.stderr
    assert run_git(repo, "rev-parse", "HEAD") == head


@needs_prek
def test_e2e_prek_commit_msg_hook_passes_and_trailers_land(
    with_msg_linter: Path,
) -> None:
    repo = with_msg_linter
    claude_write(repo, "a.txt")
    run_git(repo, "add", "a.txt")
    done = git_commit(repo, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(repo) == [None]
    assert (repo / "cog.log").read_text(encoding="utf-8") == "ran\n"


@needs_prek
def test_e2e_identity_check_fails_even_when_prek_passes(
    with_msg_linter: Path,
) -> None:
    repo = with_msg_linter
    stage(repo, "a.txt")
    done = git_commit(repo, "feat: a", CLAUDE_E2E, "--trailer", "Generated-by: robot")
    assert done.returncode != 0
    assert "must start with literal LLM" in done.stderr
    # prek's hooks still ran after the identity step failed.
    assert (repo / "cog.log").read_text(encoding="utf-8") == "ran\n"


def test_cmd_trailer_ranks_by_blob_from_a_subdirectory(
    repo: Path, tmp_path: Path
) -> None:
    stage(repo, "top.txt")
    blob = run_git(repo, "ls-files", "-s", "top.txt").split()[1]
    last = make_ident(root=SESSION_HASH, model="anthropic:last")
    stale = make_ident(CHILD, "anthropic:stale", SESSION_HASH)
    info = storage.repo_info()
    assert info is not None
    for name, who, receipt_blob, ts in (
        ("last", last, blob, 1),
        ("stale", stale, "other", 2),
    ):
        storage.write_json(
            info.state / "edits" / records.hx(SESSION_HASH) / f"{name}.json",
            make_receipt("top.txt", who, receipt_blob, ts),
        )
    (repo / "sub").mkdir()
    os.chdir(repo / "sub")
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    generated = [ln for ln in lines if ln.startswith("Generated-by:")]
    assert "model=anthropic:last" in generated[0]
    assert "model=anthropic:stale" in generated[1]


@pytest.mark.usefixtures("repo")
def test_run_trailer_passes_a_merge_commit(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    env = CLAUDE_COMMIT_ENV | {commit_policy.RUN_HOOK_ENV: "1"}
    assert commit_hooks.trailer_command(str(path), "merge", env) == 0
    assert "Generated-by: LLM " in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo")
def test_run_trailer_refuses_a_successful_noop(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    env = CLAUDE_COMMIT_ENV | {commit_policy.RUN_HOOK_ENV: "1"}
    with pytest.raises(
        records.AttributionError, match=r"^run commit requires a Generated-by trailer$"
    ):
        commit_hooks.trailer_command(str(path), "squash", env)
    with pytest.raises(records.AttributionError) as caught:
        commit_hooks.trailer_command(str(path), None, {commit_policy.RUN_HOOK_ENV: "1"})
    assert str(caught.value) == commit_policy.SESSION_ERROR


def test_run_trailer_preserves_a_hook_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commit_hooks, "cmd_trailer", returning(3))
    env = CLAUDE_COMMIT_ENV | {commit_policy.RUN_HOOK_ENV: "1"}
    assert commit_hooks.trailer_command(str(tmp_path / "absent"), "merge", env) == 3
    assert (
        commit_hooks.trailer_command(
            "--dry-run", None, {commit_policy.RUN_HOOK_ENV: "1"}
        )
        == 0
    )


def test_trailers_mix_hooked_edits_and_a_rollout_only_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    root, child = hook_rollouts(home, repo)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    run_hook(
        monkeypatch, hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    )
    # The child never fired a hook (untrusted, say): only its rollout shows it.
    (repo / "sub.txt").write_text("sub\n", encoding="utf-8")
    with child.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(patch_call("sub.txt")) + "\n")
    run_git(repo, "add", "root.txt", "sub.txt")
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": ROOT_THREAD,
    }
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, env) == 0
    session = records.sha256_64(ROOT_THREAD)
    assert sorted(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if ": LLM " in line
    ) == [
        (
            "Generated-by: LLM effort=high;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-sol;session={session};surface=exec"
        ),
        (
            "Generated-by: LLM effort=low;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-terra;session={session};"
            f"subagent={records.sha256_64(CHILD_THREAD)};surface=exec"
        ),
    ]


@pytest.mark.parametrize(
    ("source", "env"), [("merge", {}), ("squash", CLAUDE_COMMIT_ENV)]
)
def test_cmd_trailer_unstamped_merge_without_cherry_pick_head_returns_early(
    repo: Path, tmp_path: Path, source: str, env: dict[str, str]
) -> None:
    stage(repo, "a.txt")
    path = msg_file(tmp_path, "merge\n")
    assert commit_hooks.cmd_trailer(str(path), source, env) == 0
    assert path.read_text(encoding="utf-8") == "merge\n"


@pytest.mark.parametrize("operation", ["rm", "mv", "mode", "empty"])
def test_cmd_trailer_no_receipt_commit_cannot_suppress_committer(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    stage(repo, "a.txt")
    commit(repo)
    if operation == "rm":
        run_git(repo, "rm", "a.txt")
    elif operation == "mv":
        run_git(repo, "mv", "a.txt", "b.txt")
    elif operation == "mode":
        run_git(repo, "update-index", "--chmod=+x", "a.txt")
    monkeypatch.setattr(claude_ancestry, "ancestry", returning(["git commit -m x"]))
    forged = line_of(EARLY, f"launch={LAUNCH};", who=OTHER)
    path = msg_file(tmp_path, f"subject\n\nGenerated-by: {forged}\n")
    assert commit_hooks.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    found = identity_trailers.llm_trailers(path.read_text())
    assert any(
        identity_trailers.trailer_agent(value) == (SESSION_RAW, None)
        for _, value in found
    )
