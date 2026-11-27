# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity replay tests."""

import subprocess
from pathlib import Path

import pytest
from integration.alpha.identity.identity_test_support import (
    ASSIST_OTHER,
    CHILD,
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    EARLY,
    GEN_SUB,
    LATE,
    LAUNCH,
    SESSION_RAW,
    SUB_E2E,
    add_worktree,
    claims_of,
    claude_turn,
    claude_write,
    commit,
    committer_of,
    generated_subagents,
    git_commit,
    hook_env,
    last_message,
    make_ident,
    make_receipt,
    msg_file,
    needs_prek,
    no_start,
    put_receipt,
    returning,
    run_git,
    stage,
    trailers,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import attribution, replay, sessions, validation, values
from tbhbdev.alpha.identity import commit as identity_commit
from tbhbdev.alpha.identity import trailers as identity_trailers


def test_amended_trailers_reads_head_only_when_amending(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\n{GEN_SUB[0]}: {GEN_SUB[1]}\n")
    monkeypatch.setattr(replay, "ancestry", returning(["git commit -m x"]))
    assert replay.amended_trailers(repo) == []
    monkeypatch.setattr(replay, "ancestry", returning(["git commit --amend -m x"]))
    assert replay.amended_trailers(repo) == [GEN_SUB]


def test_cmd_trailer_amend_keeps_the_amended_commits_trailers(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``--amend -m`` dropped a subagent's consumed Generated-by."""
    session = values.sha256_64("session-raw")
    sub = f"LLM harness=claude-code;session={session};subagent={CHILD}"
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: {sub}\nAssisted-by: {ASSIST_OTHER[1]}\n")
    monkeypatch.setattr(replay, "ancestry", returning(["git commit --amend -m x"]))
    path = msg_file(tmp_path, "reworded\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    # Message only: no new edit, so no committer fallback either.
    assert path.read_text(encoding="utf-8") == (
        f"reworded\n\nGenerated-by: {sub}\nAssisted-by: {ASSIST_OTHER[1]}\n"
    )
    # New content in the amend adds its writer and keeps the rest.
    stage(repo, "b.txt")
    put_receipt(
        repo, "b.json", make_receipt("b.txt", make_ident(root=session)), session
    )
    path = msg_file(tmp_path, "reworded\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[2] == f"Generated-by: {sub}"
    assert lines[3] == (
        "Generated-by: LLM harness=claude-code:2.1.316;model=anthropic:m;"
        f"session={session};surface=cli"
    )
    assert lines[4:] == [f"Assisted-by: {ASSIST_OTHER[1]}"]


def test_cmd_trailer_human_amend_keeps_trailers_and_adds_none(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\n{GEN_SUB[0]}: {GEN_SUB[1]}\n")
    path = msg_file(tmp_path, "human\n")
    monkeypatch.setattr(replay, "ancestry", returning(["git commit -m x"]))
    assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
    assert path.read_text(encoding="utf-8") == "human\n"
    monkeypatch.setattr(replay, "ancestry", returning(["git commit --amend -m x"]))
    assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
    assert path.read_text(encoding="utf-8") == f"human\n\n{GEN_SUB[0]}: {GEN_SUB[1]}\n"


@needs_prek
@pytest.mark.parametrize("flags", [("--amend",), ("--amend", "--no-edit")])
def test_e2e_amend_keeps_the_subagent_trailer(
    wired: Path, flags: tuple[str, ...]
) -> None:
    """Regression: an amend after post-commit dropped the subagent's credit."""
    claude_write(wired, "a.txt")
    claude_write(wired, "b.txt", agent_id="agent-raw")
    run_git(wired, "add", "a.txt", "b.txt")
    assert git_commit(wired, "feat: ab", CLAUDE_E2E).returncode == 0
    before = trailers(wired)
    args = ["git", "commit", "-q", *flags]
    if "--no-edit" not in flags:
        args += ["-m", "feat: reworded"]
    done = subprocess.run(
        args, capture_output=True, text=True, check=False, cwd=wired,
        env=hook_env(CLAUDE_E2E),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert trailers(wired) == before


@needs_prek
def test_e2e_amend_with_new_root_edit_orders_generated_first(wired: Path) -> None:
    claude_turn(wired)
    claude_write(wired, "b.txt", agent_id="agent-raw")
    run_git(wired, "add", "b.txt")
    assert git_commit(wired, "feat: b", CLAUDE_E2E).returncode == 0
    assert [t.split(":")[0] for t in trailers(wired)] == ["Generated-by", "Assisted-by"]
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: ab", CLAUDE_E2E, "--amend", "--no-edit")
    assert done.returncode == 0, done.stderr
    assert sorted(generated_subagents(wired), key=str) == [None, SUB_E2E]
    assert not any(t.startswith("Assisted-by") for t in trailers(wired))


SRC_SESSION = values.sha256_64("source-session")

SRC_GEN = (
    "LLM harness=claude-code:2.1.316;model=anthropic:claude-opus-5-5;"
    f"session={SRC_SESSION};surface=cli"
)

SRC_GEN_NO_MODEL = f"LLM harness=claude-code:2.1.316;session={SRC_SESSION};surface=cli"

SHA_A = "a" * 40

SHA_B = "b" * 40

HUMAN = "Human <h@example.invalid>"


@pytest.mark.parametrize(
    ("head", "text", "expected"),
    [
        (None, "msg\n", None),
        (SHA_B, "msg\n", SHA_B),
        (SHA_B, f"msg\n\n(cherry picked from commit {SHA_A})\n", SHA_B),
        (None, f"msg\n\n(cherry picked from commit {SHA_A})\n", SHA_A),
        (
            None,
            (
                f"(cherry picked from commit {SHA_A})\n\n"
                f"(cherry picked from commit {SHA_B})\n"
            ),
            SHA_B,
        ),
        (None, f"(cherry picked from commit {SHA_A[:39]})\n", None),
        (None, f"(cherry picked from commit {SHA_A.upper()})\n", None),
        (None, f"see (cherry picked from commit {SHA_A})\n", None),
        (None, f"(cherry picked from commit {SHA_A}) x\n", None),
        (
            None,
            f"(cherry picked from commit {SHA_A})\n(cherry picked from commit xyz)\n",
            SHA_A,
        ),
    ],
)
def test_pick_source(head: str | None, text: str, expected: str | None) -> None:
    assert replay.pick_source(head, text) == expected


@pytest.mark.parametrize(
    ("trailers_", "own", "expected"),
    [
        ([], True, True),
        ([], False, False),
        ([(identity_trailers.KEY_GEN, SRC_GEN)], True, False),
        ([(identity_trailers.KEY_GEN, SRC_GEN)], False, False),
        ([(identity_trailers.KEY_GEN, SRC_GEN_NO_MODEL)], True, True),
        ([(identity_trailers.KEY_GEN, SRC_GEN_NO_MODEL)], False, False),
        ([(identity_trailers.KEY_ASSIST, SRC_GEN)], True, True),
        ([(identity_trailers.KEY_GEN, "LLM harness=x;not-model=y")], True, True),
        ([(identity_trailers.KEY_GEN, "LLM harness=x;model=")], True, True),
        (
            [
                (identity_trailers.KEY_GEN, SRC_GEN_NO_MODEL),
                (identity_trailers.KEY_GEN, SRC_GEN),
            ],
            True,
            False,
        ),
    ],
)
def test_pick_refused(
    trailers_: list[validation.Trailer], own: bool, expected: bool
) -> None:
    assert replay.pick_refused(trailers_, own=own) is expected


def test_usable_trailers_drops_a_generated_by_without_a_model() -> None:
    gen = (identity_trailers.KEY_GEN, SRC_GEN)
    bare = (identity_trailers.KEY_GEN, SRC_GEN_NO_MODEL)
    assert replay.usable_trailers([bare, gen, ASSIST_OTHER]) == [gen, ASSIST_OTHER]
    assert replay.usable_trailers([]) == []


@pytest.mark.parametrize(("edited", "assisted"), [(True, True), (False, False)])
def test_pick_roles_never_make_the_picker_generated(
    edited: bool, assisted: bool
) -> None:
    who = committer_of(make_ident())
    assert replay.pick_roles(who, edited=edited) == (
        [],
        [who.ident] if assisted else [],
    )


def test_picked_commit_reads_the_source_commit(repo: Path) -> None:
    stage(repo, "old.txt")
    commit(repo, f"older\n\nGenerated-by: {SRC_GEN_NO_MODEL}\n")  # not the source
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: {SRC_GEN}\nAssisted-by: {ASSIST_OTHER[1]}\n")
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    expected = (
        sha,
        [(identity_trailers.KEY_GEN, SRC_GEN), ASSIST_OTHER],
        "agent@tonyburns.net",
    )
    assert replay.picked_commit(sha, "x\n", repo) == expected
    assert (
        replay.picked_commit(None, f"(cherry picked from commit {sha})\n", repo)
        == expected
    )
    assert replay.picked_commit(None, "x\n", repo) is None
    assert replay.picked_commit(SHA_A, "x\n", repo) is None  # not in this repository
    assert (
        replay.picked_commit(None, f"(cherry picked from commit {SHA_A})\n", repo)
        is None
    )


def test_picked_commit_reads_a_trailer_in_the_first_paragraph(repo: Path) -> None:
    stage(repo, "a.txt")
    commit(
        repo, f"s\nGenerated-by: {SRC_GEN}"
    )  # no blank line: all of it is the subject
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    assert replay.picked_commit(sha, "x\n", repo) == (
        sha,
        [(identity_trailers.KEY_GEN, SRC_GEN)],
        "agent@tonyburns.net",
    )


def test_cmd_trailer_pick_by_x_line_without_cherry_pick_head(
    repo: Path, tmp_path: Path
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: {SRC_GEN}\n")
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    path = msg_file(tmp_path, f"s\n\n(cherry picked from commit {sha})\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    text = path.read_text(encoding="utf-8")
    assert validation.llm_trailers(text) == [(identity_trailers.KEY_GEN, SRC_GEN)]
    assert f"(cherry picked from commit {sha})" in text


def test_cmd_trailer_x_line_naming_a_missing_commit_is_not_a_pick(
    repo: Path, tmp_path: Path
) -> None:
    stage(repo, "a.txt")
    path = msg_file(tmp_path, f"s\n\n(cherry picked from commit {SHA_A})\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    (line,) = validation.llm_trailers(path.read_text(encoding="utf-8"))
    assert (
        line[0] == identity_trailers.KEY_GEN
    )  # the committer fallback, as for any commit


def side_commit(
    repo: Path,
    message: str,
    name: str = "s.txt",
    content: str | None = None,
    author: str | None = None,
) -> str:
    """A commit on a new branch `side` off the current one; return its sha."""
    base = run_git(repo, "branch", "--show-current").strip()
    run_git(repo, "switch", "-q", "-c", "side")
    stage(repo, name, content)
    flags = [f"--author={author}"] if author else []
    run_git(repo, "commit", "-q", "--no-verify", *flags, "-m", message)
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "switch", "-q", base)
    return sha


def conflicting_source(repo: Path, message: str, author: str | None = None) -> str:
    """A `side` commit and a main commit that both change base.txt."""
    sha = side_commit(repo, message, "base.txt", "side\n", author)
    stage(repo, "base.txt", "main\n")
    run_git(repo, "commit", "-q", "--no-verify", "-m", "main change")
    return sha


def git_run(
    repo: Path, *args: str, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.editor=true", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
        env=hook_env({**CLAUDE_E2E, **(extra or {})}),
    )


def resolve(repo: Path) -> None:
    claude_write(repo, "base.txt", "resolved\n")
    run_git(repo, "add", "base.txt")


@needs_prek
def test_e2e_pick_keeps_the_source_trailer_and_adds_nothing(wired: Path) -> None:
    """Guards the plain pick: the message already carries the source's trailer."""
    sha = side_commit(wired, f"feat: s\n\nGenerated-by: {SRC_GEN}")
    done = git_run(wired, "cherry-pick", "-x", sha)
    assert done.returncode == 0, done.stderr
    assert trailers(wired) == [f"Generated-by: {SRC_GEN}"]
    assert f"(cherry picked from commit {sha})" in last_message(wired)


@needs_prek
def test_e2e_multi_commit_pick_keeps_each_sources_trailer(wired: Path) -> None:
    first = side_commit(wired, f"feat: s\n\nGenerated-by: {SRC_GEN}")
    run_git(wired, "switch", "-q", "side")
    stage(wired, "t.txt")
    run_git(
        wired,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        f"feat: t\n\nGenerated-by: {SRC_GEN}",
    )
    run_git(wired, "switch", "-q", "-")
    done = git_run(wired, "cherry-pick", f"{first}~1..side")
    assert done.returncode == 0, done.stderr
    assert (
        run_git(wired, "log", "-2", "--format=%b").count(f"Generated-by: {SRC_GEN}")
        == 2
    )


@needs_prek
def test_e2e_conflict_resolution_credits_the_picker_as_assisted(wired: Path) -> None:
    sha = conflicting_source(wired, f"feat: s\n\nGenerated-by: {SRC_GEN}")
    assert git_run(wired, "cherry-pick", sha).returncode != 0
    resolve(wired)
    done = git_run(wired, "cherry-pick", "--continue")
    assert done.returncode == 0, done.stderr
    session = values.sha256_64("session-raw")
    assert no_start(trailers(wired)) == [
        f"Generated-by: {SRC_GEN}",
        (
            "Assisted-by: LLM harness=claude-code:2.1.316;"
            f"model=anthropic:claude-opus-5-5;session={session};surface=cli"
        ),
    ]


@needs_prek
@pytest.mark.parametrize("how", ["commit-m", "commit-F", "merge-msg"])
def test_e2e_a_pick_whose_message_lost_its_trailer_gets_it_back(
    wired: Path, tmp_path: Path, how: str
) -> None:
    sha = conflicting_source(wired, f"feat: s\n\nGenerated-by: {SRC_GEN}")
    assert git_run(wired, "cherry-pick", sha).returncode != 0
    resolve(wired)
    if how == "commit-m":
        done = git_run(wired, "commit", "-q", "-m", "feat: reworded")
    elif how == "commit-F":
        (tmp_path / "m.txt").write_text("feat: reworded\n", encoding="utf-8")
        done = git_run(wired, "commit", "-q", "-F", str(tmp_path / "m.txt"))
    else:
        (wired / ".git" / "MERGE_MSG").write_text("feat: reworded\n", encoding="utf-8")
        done = git_run(wired, "cherry-pick", "--continue")
    assert done.returncode == 0, done.stderr
    assert last_message(wired).startswith("feat: reworded\n")
    assert [t.split(":")[0] for t in trailers(wired)] == ["Generated-by", "Assisted-by"]
    assert trailers(wired)[0] == f"Generated-by: {SRC_GEN}"


def refusal(sha: str) -> str:
    short = sha[:7]
    return (
        f"cherry-pick of {short} has no Generated-by trailer (with model=); add the "
        f"source's trailer by hand (git log -1 --format=%B {short}) and commit again"
    )


@needs_prek
@pytest.mark.parametrize(
    "message",
    ["feat: s", f"feat: s\n\nGenerated-by: {SRC_GEN_NO_MODEL}"],
    ids=["no-trailer", "no-model"],
)
def test_e2e_pick_of_an_agent_commit_without_a_model_is_refused(
    wired: Path, message: str
) -> None:
    sha = side_commit(wired, message)
    before = run_git(wired, "rev-parse", "HEAD")
    done = git_run(wired, "cherry-pick", sha)
    assert done.returncode != 0
    assert refusal(sha) in done.stderr.splitlines()
    assert run_git(wired, "rev-parse", "HEAD") == before
    # Adding the source's trailer by hand lets the commit through.
    hand = f"feat: s\n\nGenerated-by: {SRC_GEN}"
    done = git_run(wired, "commit", "-q", "-m", hand)
    assert done.returncode == 0, done.stderr
    assert trailers(wired) == [f"Generated-by: {SRC_GEN}"]


@needs_prek
@pytest.mark.parametrize("receipts", [False, True], ids=["no-receipts", "receipts"])
def test_e2e_pick_of_a_humans_commit_is_never_generated_by_the_picker(
    wired: Path, receipts: bool
) -> None:
    """A human's commit carries no trailer, so the pick adds none unless assisted."""
    sha = conflicting_source(wired, "feat: h", HUMAN)
    assert git_run(wired, "cherry-pick", sha).returncode != 0
    if receipts:
        resolve(wired)
    else:
        stage(wired, "base.txt", "resolved\n")
    # The author is the human's, which an agent commit may not keep.
    done = git_run(wired, "commit", "-q", "--reset-author", "-m", "feat: h")
    assert done.returncode == 0, done.stderr
    assert [t.split(":")[0] for t in trailers(wired)] == (
        ["Assisted-by"] if receipts else []
    )


def picking(repo: Path, message: str, author: str | None = None) -> str:
    """Commit `message`, then mark it as the commit being picked (CHERRY_PICK_HEAD)."""
    stage(repo, "src.txt")
    flags = [f"--author={author}"] if author else []
    run_git(repo, "commit", "-q", "--no-verify", *flags, "-m", message)
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    stage(repo, "a.txt")
    (repo / ".git" / "CHERRY_PICK_HEAD").write_text(sha + "\n", encoding="utf-8")
    return sha


@pytest.mark.parametrize(
    "message", ["s", f"s\n\nGenerated-by: {SRC_GEN_NO_MODEL}"], ids=["none", "no-model"]
)
def test_cmd_trailer_refuses_our_agents_pick_without_a_model(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    sha = picking(repo, message)
    text = "s\n"
    path = msg_file(tmp_path, text)
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 1
    assert capsys.readouterr().err == refusal(sha) + "\n"
    assert path.read_text(encoding="utf-8") == text
    assert claims_of(repo) == {}
    # A model-bearing Generated-by the picker adds by hand satisfies it, and the
    # source's own model-less line is not brought back next to it.
    path = msg_file(tmp_path, f"s\n\nGenerated-by: {SRC_GEN}\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert validation.llm_trailers(path.read_text(encoding="utf-8")) == [
        (identity_trailers.KEY_GEN, SRC_GEN)
    ]


def test_cmd_trailer_refuses_by_the_x_line_too(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = picking(repo, "s")
    (repo / ".git" / "CHERRY_PICK_HEAD").unlink()
    path = msg_file(tmp_path, f"s\n\n(cherry picked from commit {sha})\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 1
    assert capsys.readouterr().err == refusal(sha) + "\n"


def test_cmd_trailer_pick_of_another_authors_commit_is_not_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    picking(repo, "s", HUMAN)
    path = msg_file(tmp_path, "s\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert capsys.readouterr().err == ""
    assert path.read_text(encoding="utf-8") == "s\n"


def test_cmd_trailer_pick_restores_the_source_trailers(
    repo: Path, tmp_path: Path
) -> None:
    picking(repo, f"s\n\nGenerated-by: {SRC_GEN}\nAssisted-by: {ASSIST_OTHER[1]}\n")
    path = msg_file(tmp_path, "hand\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert path.read_text(encoding="utf-8") == (
        f"hand\n\nGenerated-by: {SRC_GEN}\nAssisted-by: {ASSIST_OTHER[1]}\n"
    )
    assert claims_of(repo)  # the committer's (empty) claim, as for any commit
    (claim,) = claims_of(repo).values()
    assert claim["receipts"] == []


@pytest.mark.parametrize("source", ["message", "merge"])
def test_cmd_trailer_pick_credits_the_picker_only_as_assisted(
    repo: Path, tmp_path: Path, source: str
) -> None:
    picking(repo, f"s\n\nGenerated-by: {SRC_GEN}\n")
    put_receipt(
        repo,
        "a.json",
        make_receipt("a.txt", make_ident(root=SESSION_RAW)),
        SESSION_RAW,
    )
    path = msg_file(tmp_path, f"s\n\nGenerated-by: {SRC_GEN}\n")
    assert identity_commit.cmd_trailer(str(path), source, CLAUDE_COMMIT_ENV) == 0
    committer = attribution.committer_identity(CLAUDE_COMMIT_ENV)
    assert committer is not None
    assert validation.llm_trailers(path.read_text(encoding="utf-8")) == [
        (identity_trailers.KEY_GEN, SRC_GEN),
        (identity_trailers.KEY_ASSIST, values.format_value(committer.ident.attrs())),
    ]
    (claim,) = claims_of(repo).values()
    assert claim["receipts"] == ["a.json"]


@pytest.mark.parametrize(
    ("source", "env"), [("merge", {}), ("squash", CLAUDE_COMMIT_ENV)]
)
def test_cmd_trailer_unstamped_merge_without_cherry_pick_head_returns_early(
    repo: Path, tmp_path: Path, source: str, env: dict[str, str]
) -> None:
    stage(repo, "a.txt")
    path = msg_file(tmp_path, "merge\n")
    assert identity_commit.cmd_trailer(str(path), source, env) == 0
    assert path.read_text(encoding="utf-8") == "merge\n"


def test_cmd_trailer_pick_asks_git_in_the_repository_top(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact CHERRY_PICK_HEAD lookup, and every other call anchored at the top."""
    picking(repo, f"s\n\nGenerated-by: {SRC_GEN}\n")
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    monkeypatch.setattr(replay, "ancestry", returning(["git commit --amend -m x"]))
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    real = git_repo.git

    def spy(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        calls.append((args, cwd))
        return real(*args, cwd=cwd)

    monkeypatch.setattr(replay, "git", spy)
    path = msg_file(tmp_path, "hand\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert calls[1] == (("rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"), None)
    anchored = [cwd for args, cwd in calls if args[0] in {"log", "var"}]
    assert anchored == [repo, repo, repo]  # amend's HEAD, the source, the committer


ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": "/dev/null"}


def moved_on(repo: Path, message: str) -> str:
    """A `side` commit by our own identity, checked out, with main a commit ahead."""
    sha = side_commit(repo, message)
    stage(repo, "m.txt")
    run_git(repo, "commit", "-q", "--no-verify", "-m", "main moves on")
    run_git(repo, "switch", "-q", "side")
    return sha


@needs_prek
@pytest.mark.parametrize(("backend", "added"), [("--merge", True), ("--apply", False)])
def test_e2e_rebase_replays_are_not_picks(
    wired: Path, backend: str, added: bool
) -> None:
    """Git sets CHERRY_PICK_HEAD for each commit a merge-backend rebase replays.

    The trailers are those the hook gave a replayed commit before it knew about
    picks: the committer's fallback line from the merge backend, which runs the
    hook, and nothing from the apply backend, which doesn't.
    """
    base = run_git(wired, "branch", "--show-current").strip()
    moved_on(wired, "feat: s")
    main = run_git(wired, "rev-parse", base).strip()
    done = git_run(wired, "rebase", backend, base, extra=ISOLATED_GIT)
    assert done.returncode == 0, done.stderr
    assert "cherry-pick" not in done.stderr
    assert run_git(wired, "rev-parse", "HEAD~1").strip() == main
    session = values.sha256_64("session-raw")
    assert trailers(wired) == (
        [f"Generated-by: LLM harness=claude-code:2.1.316;session={session};surface=cli"]
        if added
        else []
    )


@needs_prek
@pytest.mark.parametrize("backend", ["--merge", "--apply"])
def test_e2e_rebase_continues_after_a_conflict(wired: Path, backend: str) -> None:
    base = run_git(wired, "branch", "--show-current").strip()
    conflicting_source(wired, "feat: s")
    run_git(wired, "switch", "-q", "side")
    assert git_run(wired, "rebase", backend, base, extra=ISOLATED_GIT).returncode != 0
    resolve(wired)
    done = git_run(wired, "rebase", "--continue", extra=ISOLATED_GIT)
    assert done.returncode == 0, done.stderr
    assert "cherry-pick" not in done.stderr
    assert run_git(wired, "show", "HEAD:base.txt") == "resolved\n"


@needs_prek
def test_e2e_the_rebased_commit_is_still_refused_as_a_pick(wired: Path) -> None:
    base = run_git(wired, "branch", "--show-current").strip()
    sha = moved_on(wired, "feat: s")
    assert git_run(wired, "rebase", base, extra=ISOLATED_GIT).returncode == 0
    run_git(wired, "switch", "-q", base)
    before = run_git(wired, "rev-parse", "HEAD")
    done = git_run(wired, "cherry-pick", sha, extra=ISOLATED_GIT)
    assert done.returncode != 0
    assert refusal(sha) in done.stderr.splitlines()
    assert run_git(wired, "rev-parse", "HEAD") == before


@pytest.mark.parametrize("name", ["rebase-merge", "rebase-apply"])
@pytest.mark.parametrize("cwd", ["", "sub"])
def test_rebasing_sees_either_rebase_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch, name: str, cwd: str
) -> None:
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / cwd)
    assert not replay.rebasing()
    (repo / ".git" / name).mkdir()
    assert replay.rebasing()


def test_rebasing_asks_git_for_the_exact_directory_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact names: a case-insensitive file system finds a directory in any case."""
    asked: list[tuple[str, ...]] = []

    def spy(*args: str, **_: object) -> tuple[int, str]:
        asked.append(args)
        return 0, ""

    monkeypatch.setattr(replay, "git", spy)
    assert not replay.rebasing()
    assert asked == [
        ("rev-parse", "--git-path", "rebase-merge", "--git-path", "rebase-apply")
    ]


@pytest.mark.usefixtures("plain")
def test_rebasing_outside_a_repository_is_false() -> None:
    assert not replay.rebasing()


def test_rebasing_in_a_linked_worktree_reads_its_own_state(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = add_worktree(repo, tmp_path / "linked")
    monkeypatch.chdir(tree)
    assert not replay.rebasing()
    (repo / ".git" / "rebase-merge").mkdir()  # the main work tree's rebase
    assert not replay.rebasing()
    git_dir = Path(run_git(tree, "rev-parse", "--absolute-git-dir").strip())
    (git_dir / "rebase-merge").mkdir()
    assert replay.rebasing()


@pytest.mark.parametrize("name", ["rebase-merge", "rebase-apply"])
@pytest.mark.parametrize("source", ["message", "merge"])
def test_cmd_trailer_in_a_rebase_is_never_a_pick(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    source: str,
) -> None:
    """Our agent's commit with no trailer, replayed: no refusal, no pick handling."""
    sha = picking(repo, "s")
    (repo / ".git" / name).mkdir()
    text = f"s\n\n(cherry picked from commit {sha})\n"
    path = msg_file(tmp_path, text)
    assert identity_commit.cmd_trailer(str(path), source, CLAUDE_COMMIT_ENV) == 0
    assert capsys.readouterr().err == ""
    after = path.read_text(encoding="utf-8")
    if source == "merge":  # the early return, as without any pick
        assert after == text
    else:  # the committer's fallback, as for any commit
        (line,) = validation.llm_trailers(after)
        assert line[0] == identity_trailers.KEY_GEN
        assert claims_of(repo)


def test_cmd_trailer_amend_takes_the_start_from_the_record(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = values.sha256_64("session-raw")
    base = f"harness=claude-code:2.1.316;model=anthropic:m;session={session}"
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: LLM {base};started_at={EARLY};surface=cli\n")
    sessions.write_record(session, {}, once={"started_at": LATE, "launch": LAUNCH})
    monkeypatch.setattr(replay, "ancestry", returning(["git commit --amend -m x"]))
    hook = (
        f"Generated-by: LLM harness=claude-code:2.1.316;launch={LAUNCH};"
        f"model=anthropic:m;session={session};started_at={LATE};surface=cli\n"
    )
    stage(repo, "b.txt")
    put_receipt(
        repo, "b.json", make_receipt("b.txt", make_ident(root=session)), session
    )
    path = msg_file(tmp_path, "reworded\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    # The hook's line replaces the amended line of its own agent.
    assert path.read_text(encoding="utf-8") == f"reworded\n\n{hook}"
    # With no receipt the amended line still takes the record's fields.
    commit(repo, f"s\n\nGenerated-by: LLM {base};surface=cli\n")
    path = msg_file(tmp_path, "reworded\n")
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert path.read_text(encoding="utf-8") == f"reworded\n\n{hook}"
    assert validation.check_message(path.read_text(encoding="utf-8")) == []
