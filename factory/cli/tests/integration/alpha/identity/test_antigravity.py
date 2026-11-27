# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity antigravity tests."""

import itertools
import json
import time
from typing import TYPE_CHECKING

import pytest
from integration.alpha.identity.identity_test_support import (
    CONV,
    EARLY,
    LAUNCH,
    NOW,
    Json,
    agy_post,
    feed,
    loaded,
    msg_file,
    no_start,
    receipts_of,
    returning,
    spy_dirs,
    stage,
)

from tbhbdev.alpha.identity import antigravity, attribution, sessions, storage, values
from tbhbdev.alpha.identity import commit as identity_commit
from tbhbdev.alpha.identity import record as identity_record
from tbhbdev.alpha.identity import trailers as identity_trailers

if TYPE_CHECKING:
    from pathlib import Path

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
    assert antigravity.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "read.json", invoke(CONV, SUB))
    assert antigravity.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "a.txt", invoke(CONV, SUB))
    write_message(brain, SUB, "b.json", "not json")
    write_message(brain, SUB, "c.json", {"recipient": SUB})
    write_message(brain, SUB, "d.json", invoke(CONV, "someone-else"))
    write_message(brain, SUB, "e.json", invoke(CONV, SUB, "other_tool"))
    write_message(brain, SUB, "f.json", [1])
    assert antigravity.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "g.json", invoke(CONV, SUB))
    assert antigravity.agy_parent(brain, SUB) == CONV
    write_message(brain, SUB, "g.json", invoke(None, SUB))
    assert antigravity.agy_parent(brain, SUB) == "tool-sender"
    write_message(brain, SUB, "g.json", invoke(SUB, SUB))
    assert antigravity.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "g.json", invoke("", SUB))
    assert antigravity.agy_parent(brain, SUB) == "tool-sender"


def test_agy_root_walks_up_safely(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    assert antigravity.agy_root(brain, CONV) == CONV
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    write_message(brain, GRAND, "m.json", invoke(SUB, GRAND))
    assert antigravity.agy_root(brain, SUB) == CONV
    assert antigravity.agy_root(brain, GRAND) == CONV
    assert antigravity.agy_root(brain, GRAND, max_depth=1) == SUB
    assert antigravity.agy_root(brain, GRAND, max_depth=0) == GRAND
    write_message(brain, CONV, "m.json", invoke(GRAND, CONV))  # a cycle
    assert antigravity.agy_root(brain, GRAND) in {CONV, SUB, GRAND}
    assert antigravity.agy_root(brain, SUB, max_depth=100) == GRAND


def test_agy_root_depth_default(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    names = [f"c{i}" for i in range(12)]
    for child, parent in itertools.pairwise(names):
        write_message(brain, child, "m.json", invoke(parent, child))
    assert antigravity.agy_root(brain, names[0]) == names[8]


def test_agy_brain_root(tmp_path: Path) -> None:
    recorded = tmp_path / "recorded"
    appdata = tmp_path / "appdata"
    (appdata / "brain").mkdir(parents=True)
    default = tmp_path / "home" / ".gemini" / "antigravity-cli" / "brain"
    assert antigravity.agy_brain_root({}, {}) is None
    default.mkdir(parents=True)
    assert antigravity.agy_brain_root({}, {}) == default
    env = {"ANTIGRAVITY_APP_DATA_DIR": str(appdata)}
    assert antigravity.agy_brain_root({}, env) == appdata / "brain"
    assert (
        antigravity.agy_brain_root({"brain_root": str(recorded)}, env)
        == appdata / "brain"
    )
    recorded.mkdir()
    assert antigravity.agy_brain_root({"brain_root": str(recorded)}, env) == recorded
    assert antigravity.agy_brain_root({"brain_root": 3}, {}) == default
    missing = {"ANTIGRAVITY_APP_DATA_DIR": str(tmp_path / "nope")}
    assert antigravity.agy_brain_root({}, missing) == default


def test_agy_tool_and_edit_path() -> None:
    assert antigravity.agy_tool({"toolCall": {"name": "w", "args": {"a": 1}}}) == (
        "w",
        {"a": 1},
    )
    assert antigravity.agy_tool({"toolName": "w", "toolInput": "x"}) == ("w", "x")
    assert antigravity.agy_tool({"tool_name": "w"}) == ("w", None)
    assert antigravity.agy_tool({"toolCall": "x", "tool_name": "w"}) == ("w", None)
    assert antigravity.agy_tool({"toolCall": {"name": "n"}, "toolName": "x"}) == (
        "n",
        None,
    )
    assert antigravity.agy_tool({}) == (None, None)
    assert antigravity.agy_edit_path({"Path": "a"}) == "a"
    assert antigravity.agy_edit_path(json.dumps({"TargetFile": "t"})) == "t"
    assert antigravity.agy_edit_path("{") is None
    assert antigravity.agy_edit_path("[1]") is None
    assert antigravity.agy_edit_path(None) is None
    assert antigravity.agy_edit_path({"Path": "", "path": "p"}) == "p"
    assert antigravity.agy_edit_path({"other": "x"}) is None
    for key in antigravity.AGY_PATH_KEYS:
        assert antigravity.agy_edit_path({key: "k"}) == "k"


def test_agy_ident(tmp_path: Path) -> None:
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    rec: Json = {"model": "gemini-3.8-flash-low"}
    got = antigravity.agy_ident(CONV, rec, env)
    assert got == values.Ident(
        values.sha256_64(CONV),
        "antigravity:1.4.12",
        None,
        "gcp.gemini:gemini-3.8-flash",
        "low",
        "cli",
    )
    assert antigravity.agy_ident(
        CONV, {"model": "m", "effort": "high"}, {}
    ) == values.Ident(values.sha256_64(CONV), "antigravity", None, "m", "high", None)
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    child = antigravity.agy_ident(SUB, rec, env, brain)
    assert (child.root, child.subagent) == (
        values.sha256_64(CONV),
        values.sha256_64(SUB),
    )
    top = antigravity.agy_ident(CONV, rec, env, brain)
    assert (top.root, top.subagent) == (values.sha256_64(CONV), None)


def test_cmd_record_agy_edit(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    brain = repo.parent / "brain"
    brain.mkdir()
    data = agy_post(
        workspacePaths=[str(repo)], artifactDirectoryPath=f"{brain / CONV}/"
    )
    feed(monkeypatch, data)
    assert identity_record.cmd_record("antigravity", env) == 0
    rec = sessions.load_record(values.sha256_64(CONV))
    assert rec["harness"] == "antigravity"
    assert rec["model"] == "gemini-3.8-flash-low"
    assert rec["brain_root"] == str(brain)
    name = f"{values.hx(values.sha256_64(CONV))}.json"
    expected = {
        "harness": "antigravity",
        "model": "gemini-3.8-flash-low",
        "brain_root": str(brain),
    }
    assert no_start(loaded(storage.user_store() / name)) == expected
    assert (
        no_start(loaded(repo / ".git" / "tbhb-alpha" / "sessions" / name)) == expected
    )
    (got,) = receipts_of(repo, values.sha256_64(CONV))
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
        assert identity_record.cmd_record("antigravity", {}) == 0
    assert receipts_of(repo, values.sha256_64(CONV)) == []
    assert sessions.load_record(values.sha256_64(CONV))["harness"] == "antigravity"


def test_cmd_record_agy_without_workspace_uses_cwd(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = {k: v for k, v in agy_post().items() if k != "workspacePaths"}
    feed(monkeypatch, data)
    assert identity_record.cmd_record("antigravity", {}) == 0
    assert len(receipts_of(repo, values.sha256_64(CONV))) == 1


def test_agy_committer(repo: Path, tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    sessions.write_record(
        values.sha256_64(CONV),
        {"brain_root": str(brain), "model": "gemini-3.8-flash-high"},
        repo,
    )
    sessions.write_record(
        values.sha256_64(SUB),
        {"brain_root": str(brain), "model": "claude-opus-5-5-low"},
        repo,
    )
    env = {"ANTIGRAVITY_CONVERSATION_ID": SUB, "ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.key == values.ANTIGRAVITY
    assert got.ident == values.Ident(
        values.sha256_64(CONV),
        "antigravity:1.4.12",
        values.sha256_64(SUB),
        "anthropic:claude-opus-5-5",
        "low",
        "cli",
    )
    assert got.root_ident == values.Ident(
        values.sha256_64(CONV),
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
        == values.Ident(values.sha256_64(CONV), "antigravity")
    )


def test_agy_parent_reads_the_documented_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_dirs(monkeypatch)
    antigravity.agy_parent(tmp_path, SUB)
    assert seen == [tmp_path / SUB / ".system_generated" / "messages"]


def test_agy_root_remembers_every_conversation_it_visited(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_message(brain, "a", "m.json", invoke("b", "a"))
    write_message(brain, "b", "m.json", invoke("c", "b"))
    write_message(brain, "c", "m.json", invoke("b", "c"))
    assert antigravity.agy_root(brain, "a", max_depth=3) == "c"


def test_cmd_record_agy_child_follows_the_hook_cwd_and_brain(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = agy_post(
        conversationId=SUB, cwd=str(repo), artifactDirectoryPath=f"{brain / SUB}/"
    )
    feed(monkeypatch, data)
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    assert identity_record.cmd_record("antigravity", env) == 0
    assert sessions.load_record(values.sha256_64(SUB), repo)["brain_root"] == str(brain)
    (got,) = receipts_of(repo, values.sha256_64(CONV))
    assert got["subagent"] == values.sha256_64(SUB)
    assert got["harness"] == "antigravity:1.4.12"
    assert plain.is_dir()


@pytest.mark.usefixtures("repo")
def test_agy_committer_keys_and_roots(tmp_path: Path) -> None:
    solo = attribution.committer_identity({"ANTIGRAVITY_CONVERSATION_ID": CONV})
    assert solo is not None
    assert solo.key == values.ANTIGRAVITY
    assert solo.ident == solo.root_ident
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    sessions.write_record(values.sha256_64(SUB), {"brain_root": str(brain)})
    child = attribution.committer_identity({"ANTIGRAVITY_CONVERSATION_ID": SUB})
    assert child is not None
    assert child.key == values.ANTIGRAVITY
    assert child.ident.subagent == values.sha256_64(SUB)
    assert child.root_ident.subagent is None
    assert child.root_ident.root == values.sha256_64(CONV)


def test_agy_1_5_7_session_record_renders_the_spike_trailer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spike S-000001 (agy 1.5.7): a hook with no version or surface in its env.

    The hook stores the raw `modelName` key. The commit's env supplies the
    version and surface, and the trailer maps the model and effort.
    """
    data = agy_post(workspacePaths=[str(repo)], modelName="gemini-4-argon-high")
    feed(monkeypatch, data)
    assert identity_record.cmd_record("antigravity", {}) == 0
    rec = sessions.load_record(values.sha256_64(CONV))
    assert rec["model"] == "gemini-4-argon-high"
    commit_env = {"ANTIGRAVITY_LS_VERSION": "cli-1.5.7"}
    ident = antigravity.agy_ident(CONV, rec, commit_env)
    session = values.sha256_64(CONV)
    assert values.format_value(ident.attrs()) == (
        "LLM effort=high;harness=antigravity:1.5.7;model=gcp.gemini:gemini-4-argon;"
        f"session={session};surface=cli"
    )


def test_agy_1_5_7_commit_path_stamps_the_spike_trailer(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = agy_post(workspacePaths=[str(repo)], modelName="gemini-4-argon-high")
    feed(monkeypatch, data)
    assert identity_record.cmd_record("antigravity", {}) == 0
    stage(repo, "hello.txt", "hi\n")
    env = {
        "ANTIGRAVITY_CONVERSATION_ID": CONV,
        "ANTIGRAVITY_LS_VERSION": "cli-1.5.7",
    }
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), None, env) == 0
    session = values.sha256_64(CONV)
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
    assert identity_record.cmd_record("antigravity", {}) == 0
    sid = values.sha256_64(CONV)
    want = {"launch": LAUNCH, "started_at": EARLY}
    assert {k: sessions.load_record(sid).get(k) for k in want} == want
    assert loaded(launch_file)["session"] == sid
    monkeypatch.setattr(time, "time", returning(NOW + 60))
    feed(monkeypatch, data)
    assert identity_record.cmd_record("antigravity", {}) == 0
    assert {k: sessions.load_record(sid).get(k) for k in want} == want
    name = f"{values.hx(sid)}.json"
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
    assert identity_record.cmd_record("antigravity", {}) == 0
    kid = sessions.load_record(values.sha256_64(SUB), repo, root=values.sha256_64(CONV))
    assert kid["started_at"] == identity_trailers.utc(NOW) != EARLY
    assert "launch" not in kid
    assert "session" not in loaded(launch_file)
    root = agy_post(
        conversationId=CONV, cwd=str(repo), artifactDirectoryPath=f"{brain / CONV}/"
    )
    feed(monkeypatch, root)
    assert identity_record.cmd_record("antigravity", {}) == 0
    assert loaded(launch_file)["session"] == values.sha256_64(CONV)
    assert sessions.load_record(values.sha256_64(CONV), repo)["launch"] == LAUNCH
