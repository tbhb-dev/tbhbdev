# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for identity antigravity session."""

from __future__ import annotations

import itertools
import json
import time
lazy from pathlib import Path

import pytest
from unit.alpha.identity.identity_examples import EARLY, LAUNCH, Json
from unit.alpha.identity.identity_fixtures import (
    CONV,
    NOW,
    feed,
    git_commit,
    loaded,
    msg_file,
    needs_prek,
    no_start,
    receipts_of,
    record,
    returning,
    run_git,
    spy_dirs,
    stage,
    stdin_fixture,
    trailers,
)

from tbhbdev.alpha.identity import (
    attribution,
    dispatcher,
    models,
    records,
    sessions,
    storage,
)
from tbhbdev.alpha.identity import trailers as identity_trailers
from tbhbdev.alpha.identity.antigravity import session as antigravity_session
from tbhbdev.alpha.identity.commit import hooks as commit_hooks

SUB = "sub-raw"


GRAND = "grand-raw"


def write_message(brain: Path, cid: str, name: str, body: object) -> None:
    folder = brain / cid / ".system_generated" / "messages"
    folder.mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    (folder / name).write_text(text, encoding="utf-8")


def invoke(sender: str | None, recipient: str, name: str = "invoke_subagent") -> Json:
    tool: Json = {"toolCall": {"name": name}, "conversationId": "tool-sender"}
    msg: Json = {"recipient": recipient, "sourceMetadata": {"tool": tool}}
    if sender is not None:
        msg["sender"] = sender
    return msg


def test_agy_parent(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    assert antigravity_session.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "read.json", invoke(CONV, SUB))
    assert antigravity_session.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "a.txt", invoke(CONV, SUB))
    write_message(brain, SUB, "b.json", "not json")
    write_message(brain, SUB, "c.json", {"recipient": SUB})
    write_message(brain, SUB, "d.json", invoke(CONV, "someone-else"))
    write_message(brain, SUB, "e.json", invoke(CONV, SUB, "other_tool"))
    write_message(brain, SUB, "f.json", [1])
    assert antigravity_session.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "g.json", invoke(CONV, SUB))
    assert antigravity_session.agy_parent(brain, SUB) == CONV
    write_message(brain, SUB, "g.json", invoke(None, SUB))
    assert antigravity_session.agy_parent(brain, SUB) == "tool-sender"
    write_message(brain, SUB, "g.json", invoke(SUB, SUB))
    assert antigravity_session.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "g.json", invoke("", SUB))
    assert antigravity_session.agy_parent(brain, SUB) == "tool-sender"


def test_agy_root_walks_up_safely(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    assert antigravity_session.agy_root(brain, CONV) == CONV
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    write_message(brain, GRAND, "m.json", invoke(SUB, GRAND))
    assert antigravity_session.agy_root(brain, SUB) == CONV
    assert antigravity_session.agy_root(brain, GRAND) == CONV
    assert antigravity_session.agy_root(brain, GRAND, max_depth=1) == SUB
    assert antigravity_session.agy_root(brain, GRAND, max_depth=0) == GRAND
    write_message(brain, CONV, "m.json", invoke(GRAND, CONV))  # a cycle
    assert antigravity_session.agy_root(brain, GRAND) in {CONV, SUB, GRAND}
    assert antigravity_session.agy_root(brain, SUB, max_depth=100) == GRAND


def test_agy_root_depth_default(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    names = [f"c{i}" for i in range(12)]
    for child, parent in itertools.pairwise(names):
        write_message(brain, child, "m.json", invoke(parent, child))
    assert antigravity_session.agy_root(brain, names[0]) == names[8]


def test_agy_brain_root(tmp_path: Path) -> None:
    recorded = tmp_path / "recorded"
    appdata = tmp_path / "appdata"
    (appdata / "brain").mkdir(parents=True)
    default = tmp_path / "home" / ".gemini" / "antigravity-cli" / "brain"
    assert antigravity_session.agy_brain_root({}, {}) is None
    default.mkdir(parents=True)
    assert antigravity_session.agy_brain_root({}, {}) == default
    env = {"ANTIGRAVITY_APP_DATA_DIR": str(appdata)}
    assert antigravity_session.agy_brain_root({}, env) == appdata / "brain"
    assert (
        antigravity_session.agy_brain_root({"brain_root": str(recorded)}, env)
        == appdata / "brain"
    )
    recorded.mkdir()
    assert (
        antigravity_session.agy_brain_root({"brain_root": str(recorded)}, env)
        == recorded
    )
    assert antigravity_session.agy_brain_root({"brain_root": 3}, {}) == default
    missing = {"ANTIGRAVITY_APP_DATA_DIR": str(tmp_path / "nope")}
    assert antigravity_session.agy_brain_root({}, missing) == default


def test_agy_ident(tmp_path: Path) -> None:
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    rec: Json = {"model": "gemini-3.8-flash-low"}
    got = antigravity_session.agy_ident(CONV, rec, env)
    assert got == models.Ident(
        records.sha256_64(CONV),
        "antigravity:1.4.12",
        None,
        "gcp.gemini:gemini-3.8-flash",
        "low",
        "cli",
    )
    assert antigravity_session.agy_ident(
        CONV, {"model": "m", "effort": "high"}, {}
    ) == models.Ident(records.sha256_64(CONV), "antigravity", None, "m", "high", None)
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    child = antigravity_session.agy_ident(SUB, rec, env, brain)
    assert (child.root, child.subagent) == (
        records.sha256_64(CONV),
        records.sha256_64(SUB),
    )
    top = antigravity_session.agy_ident(CONV, rec, env, brain)
    assert (top.root, top.subagent) == (records.sha256_64(CONV), None)


def agy_post(**overrides: object) -> Json:
    return stdin_fixture("agy-posttooluse-write.json", **overrides)


def test_cmd_record_agy_edit(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    brain = repo.parent / "brain"
    brain.mkdir()
    data = agy_post(
        workspacePaths=[str(repo)], artifactDirectoryPath=f"{brain / CONV}/"
    )
    feed(monkeypatch, data)
    assert dispatcher.cmd_record("antigravity", env) == 0
    rec = sessions.load_record(records.sha256_64(CONV))
    assert rec["harness"] == "antigravity"
    assert rec["model"] == "gemini-3.8-flash-low"
    assert rec["brain_root"] == str(brain)
    name = f"{records.hx(records.sha256_64(CONV))}.json"
    expected = {
        "harness": "antigravity",
        "model": "gemini-3.8-flash-low",
        "brain_root": str(brain),
    }
    assert no_start(loaded(storage.user_store() / name)) == expected
    assert (
        no_start(loaded(repo / ".git" / "tbhb-alpha" / "sessions" / name)) == expected
    )
    (got,) = receipts_of(repo, records.sha256_64(CONV))
    assert (got["path"], got["effort"], got["surface"]) == ("hello.txt", "low", "cli")
    assert got["model"] == "gcp.gemini:gemini-3.8-flash"


def test_cmd_record_agy_ignores_non_edits(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    base = agy_post(workspacePaths=[str(repo)])
    cases = [
        {**base, "error": "boom"},
        {**base, "toolCall": {"name": "run_command", "args": {"Path": "hello.txt"}}},
        {**base, "toolCall": {"name": "write_file", "args": {"x": 1}}},
        {k: v for k, v in base.items() if k != "error"},
    ]
    for case in cases:
        feed(monkeypatch, case)
        assert dispatcher.cmd_record("antigravity", {}) == 0
    assert receipts_of(repo, records.sha256_64(CONV)) == []
    assert sessions.load_record(records.sha256_64(CONV))["harness"] == "antigravity"


def test_cmd_record_agy_without_workspace_uses_cwd(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = {k: v for k, v in agy_post().items() if k != "workspacePaths"}
    feed(monkeypatch, data)
    assert dispatcher.cmd_record("antigravity", {}) == 0
    assert len(receipts_of(repo, records.sha256_64(CONV))) == 1


@pytest.mark.usefixtures("repo")
def test_cmd_record_brain_root_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    feed(monkeypatch, {"session_id": "s"})
    dispatcher.cmd_record("antigravity", {})
    assert "brain_root" not in sessions.load_record(records.sha256_64("s"))
    feed(monkeypatch, {"session_id": "s", "artifactDirectoryPath": "/x/brain/s"})
    dispatcher.cmd_record("antigravity", {})
    assert sessions.load_record(records.sha256_64("s"))["brain_root"] == "/x/brain"


def test_agy_committer(repo: Path, tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    sessions.write_record(
        records.sha256_64(CONV),
        {"brain_root": str(brain), "model": "gemini-3.8-flash-high"},
        repo,
    )
    sessions.write_record(
        records.sha256_64(SUB),
        {"brain_root": str(brain), "model": "claude-opus-5-5-low"},
        repo,
    )
    env = {"ANTIGRAVITY_CONVERSATION_ID": SUB, "ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.key == models.ANTIGRAVITY
    assert got.ident == models.Ident(
        records.sha256_64(CONV),
        "antigravity:1.4.12",
        records.sha256_64(SUB),
        "anthropic:claude-opus-5-5",
        "low",
        "cli",
    )
    assert got.root_ident == models.Ident(
        records.sha256_64(CONV),
        "antigravity:1.4.12",
        None,
        "gcp.gemini:gemini-3.8-flash",
        "high",
        "cli",
    )
    top = attribution.committer_identity({**env, "ANTIGRAVITY_CONVERSATION_ID": CONV})
    assert top is not None
    assert top.ident is top.root_ident


@pytest.mark.usefixtures("repo")
def test_agy_committer_without_a_brain() -> None:
    env = {"ANTIGRAVITY_CONVERSATION_ID": CONV}
    got = attribution.committer_identity(env)
    assert got is not None
    assert (
        got.ident
        == got.root_ident
        == models.Ident(records.sha256_64(CONV), "antigravity")
    )


@needs_prek
def test_e2e_agy_edit_then_commit(wired: Path) -> None:
    (wired / "hello.txt").write_text("hi\n", encoding="utf-8")
    record(
        wired,
        "antigravity",
        agy_post(workspacePaths=[str(wired)]),
        {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"},
    )
    run_git(wired, "add", "hello.txt")
    env = {"ANTIGRAVITY_CONVERSATION_ID": CONV, "ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    done = git_commit(wired, "feat: hello", env)
    assert done.returncode == 0, done.stderr
    assert no_start(trailers(wired)) == [
        (
            "Generated-by: LLM effort=low;harness=antigravity:1.4.12;"
            f"model=gcp.gemini:gemini-3.8-flash;session={records.sha256_64(CONV)};"
            "surface=cli"
        )
    ]


def test_agy_parent_reads_the_documented_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_dirs(monkeypatch)
    antigravity_session.agy_parent(tmp_path, SUB)
    assert seen == [tmp_path / SUB / ".system_generated" / "messages"]


def test_agy_root_remembers_every_conversation_it_visited(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_message(brain, "a", "m.json", invoke("b", "a"))
    write_message(brain, "b", "m.json", invoke("c", "b"))
    write_message(brain, "c", "m.json", invoke("b", "c"))
    assert antigravity_session.agy_root(brain, "a", max_depth=3) == "c"


def test_cmd_record_agy_child_follows_the_hook_cwd_and_brain(
    repo: Path, plain_checkout: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = agy_post(
        conversationId=SUB, cwd=str(repo), artifactDirectoryPath=f"{brain / SUB}/"
    )
    feed(monkeypatch, data)
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    assert dispatcher.cmd_record("antigravity", env) == 0
    assert sessions.load_record(records.sha256_64(SUB), repo)["brain_root"] == str(
        brain
    )
    (got,) = receipts_of(repo, records.sha256_64(CONV))
    assert got["subagent"] == records.sha256_64(SUB)
    assert got["harness"] == "antigravity:1.4.12"
    assert plain_checkout.is_dir()


@pytest.mark.usefixtures("repo")
def test_agy_committer_keys_and_roots(tmp_path: Path) -> None:
    solo = attribution.committer_identity({"ANTIGRAVITY_CONVERSATION_ID": CONV})
    assert solo is not None
    assert solo.key == models.ANTIGRAVITY
    assert solo.ident == solo.root_ident
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    sessions.write_record(records.sha256_64(SUB), {"brain_root": str(brain)})
    child = attribution.committer_identity({"ANTIGRAVITY_CONVERSATION_ID": SUB})
    assert child is not None
    assert child.key == models.ANTIGRAVITY
    assert child.ident.subagent == records.sha256_64(SUB)
    assert child.root_ident.subagent is None
    assert child.root_ident.root == records.sha256_64(CONV)


def test_agy_1_5_7_session_record_renders_the_spike_trailer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spike S-000001 (agy 1.5.7): a hook with no version or surface in its env.

    The hook stores the raw `modelName` key. The commit's env supplies the
    version and surface, and the trailer maps the model and effort.
    """
    data = agy_post(workspacePaths=[str(repo)], modelName="gemini-4-argon-high")
    feed(monkeypatch, data)
    assert dispatcher.cmd_record("antigravity", {}) == 0
    rec = sessions.load_record(records.sha256_64(CONV))
    assert rec["model"] == "gemini-4-argon-high"
    commit_env = {"ANTIGRAVITY_LS_VERSION": "cli-1.5.7"}
    ident = antigravity_session.agy_ident(CONV, rec, commit_env)
    session = records.sha256_64(CONV)
    assert identity_trailers.format_value(ident.attrs()) == (
        "LLM effort=high;harness=antigravity:1.5.7;model=gcp.gemini:gemini-4-argon;"
        f"session={session};surface=cli"
    )


def test_agy_1_5_7_commit_path_stamps_the_spike_trailer(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = agy_post(workspacePaths=[str(repo)], modelName="gemini-4-argon-high")
    feed(monkeypatch, data)
    assert dispatcher.cmd_record("antigravity", {}) == 0
    stage(repo, "hello.txt", "hi\n")
    env = {
        "ANTIGRAVITY_CONVERSATION_ID": CONV,
        "ANTIGRAVITY_LS_VERSION": "cli-1.5.7",
    }
    path = msg_file(tmp_path)
    assert commit_hooks.cmd_trailer(str(path), None, env) == 0
    session = records.sha256_64(CONV)
    assert no_start(path.read_text(encoding="utf-8")) == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=high;harness=antigravity:1.5.7;"
        f"model=gcp.gemini:gemini-4-argon;session={session};surface=cli\n"
    )


def test_cmd_record_agy_claims_the_launch_and_keeps_the_start(
    repo: Path, launch_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    brain = repo.parent / "brain"
    brain.mkdir()
    data = agy_post(
        workspacePaths=[str(repo)], artifactDirectoryPath=f"{brain / CONV}/"
    )
    monkeypatch.setattr(time, "time", returning(NOW))
    feed(monkeypatch, data)
    assert dispatcher.cmd_record("antigravity", {}) == 0
    sid = records.sha256_64(CONV)
    want = {"launch": LAUNCH, "started_at": EARLY}
    assert {k: sessions.load_record(sid).get(k) for k in want} == want
    assert loaded(launch_file)["session"] == sid
    monkeypatch.setattr(time, "time", returning(NOW + 60))
    feed(monkeypatch, data)
    assert dispatcher.cmd_record("antigravity", {}) == 0
    assert {k: sessions.load_record(sid).get(k) for k in want} == want
    name = f"{records.hx(sid)}.json"
    for store in (
        storage.user_store() / name,
        repo / ".git" / "tbhb-alpha" / "sessions" / name,
    ):
        assert {k: loaded(store).get(k) for k in want} == want


def test_cmd_record_agy_subagent_never_claims_the_launch(
    repo: Path, launch_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child can run its hook first, so only the root conversation claims."""
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    monkeypatch.setattr(time, "time", returning(NOW))
    child = agy_post(
        conversationId=SUB, cwd=str(repo), artifactDirectoryPath=f"{brain / SUB}/"
    )
    feed(monkeypatch, child)
    assert dispatcher.cmd_record("antigravity", {}) == 0
    kid = sessions.load_record(
        records.sha256_64(SUB), repo, root=records.sha256_64(CONV)
    )
    assert kid["started_at"] == identity_trailers.utc(NOW) != EARLY
    assert "launch" not in kid
    assert "session" not in loaded(launch_file)
    root = agy_post(
        conversationId=CONV, cwd=str(repo), artifactDirectoryPath=f"{brain / CONV}/"
    )
    feed(monkeypatch, root)
    assert dispatcher.cmd_record("antigravity", {}) == 0
    assert loaded(launch_file)["session"] == records.sha256_64(CONV)
    assert sessions.load_record(records.sha256_64(CONV), repo)["launch"] == LAUNCH
