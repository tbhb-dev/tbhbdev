# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity codex tests."""

import json
import subprocess
from typing import TYPE_CHECKING, cast

import pytest
from integration.alpha.identity.identity_test_support import (
    CHILD_THREAD,
    NOREPLY,
    ROOT_THREAD,
    SIGNOFF_BASE,
    Json,
    git_commit,
    hook_data,
    hook_env,
    hook_rollouts,
    last_message,
    meta,
    msg_file,
    needs_prek,
    no_start,
    patch_call,
    receipts_of,
    record,
    run_git,
    run_hook,
    stage,
    trailers,
    turn,
    write_rollout,
)

from tbhbdev.alpha.identity import attribution, codex, validation, values
from tbhbdev.alpha.identity import commit as identity_commit
from tbhbdev.alpha.install import hooks as install

if TYPE_CHECKING:
    from pathlib import Path

OTHER_THREAD = "01a19ebf-ffff-7000-8000-0123456789ab"


def test_codex_committer(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": CHILD_THREAD,
        "CODEX_VERSION": "0.99.0",
    }
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, source="exec"), turn("gpt-5.6-sol", "high")],
    )
    write_rollout(
        home,
        CHILD_THREAD,
        [meta(id=CHILD_THREAD, session_id=ROOT_THREAD), turn("gpt-5.6-luna", "low")],
    )
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.key == values.CODEX
    assert got.ident == values.Ident(
        values.sha256_64(ROOT_THREAD),
        "codex:0.99.0",
        values.sha256_64(CHILD_THREAD),
        "openai:gpt-5.6-luna",
        "low",
        None,
    )
    assert got.root_ident == values.Ident(
        values.sha256_64(ROOT_THREAD),
        "codex:0.99.0",
        None,
        "openai:gpt-5.6-sol",
        "high",
        "exec",
    )
    solo = attribution.committer_identity(
        {k: v for k, v in env.items() if k != "CODEX_THREAD_ID"}
    )
    assert solo is not None
    assert solo.ident == solo.root_ident == got.root_ident


def test_codex_committer_without_rollouts(tmp_path: Path) -> None:
    env = {
        "CODEX_HOME": str(tmp_path),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": "",
    }
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.ident == values.Ident(values.sha256_64(ROOT_THREAD), "codex")


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
    assert who == f"{validation.AGENT_IDENT}\n{validation.AGENT_IDENT}\n"
    assert "Signed-off-by" not in last_message(wired)


@needs_prek
@pytest.mark.parametrize("long_rollout", [False, True])
def test_e2e_codex_rollout_edit_then_commit(
    wired: Path, tmp_path: Path, *, long_rollout: bool
) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(wired), source="exec"),
            turn("gpt-5.6-sol", "high"),
            patch_call("c.txt"),
            "x" * (1024 * 1024 + 100) if long_rollout else "",
        ],
    )
    stage(wired, "c.txt")
    env = {
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_HOME": str(home),
        "CODEX_VERSION": "0.166.0",
    }
    done = git_commit(wired, "feat: c", env)
    assert done.returncode == 0, done.stderr
    session = values.sha256_64(ROOT_THREAD)
    assert trailers(wired) == [
        (
            "Generated-by: LLM effort=high;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-sol;session={session};surface=exec"
        )
    ]


def test_codex_committer_pins_the_thread_ids(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": CHILD_THREAD,
    }
    got = attribution.committer_identity(env)
    assert got is not None
    assert got.ident.subagent == values.sha256_64(CHILD_THREAD)
    assert got.root_ident.subagent is None
    # Rollout metadata naming some other thread never overrides the env.
    stray = "01a19ebf-0000-7000-8000-000000000000"
    write_rollout(home, ROOT_THREAD, [meta(id=stray)])
    write_rollout(home, CHILD_THREAD, [meta(id=stray)])
    again = attribution.committer_identity(env)
    assert again is not None
    assert again.ident.subagent == values.sha256_64(CHILD_THREAD)
    assert again.root_ident.subagent is None


def store_of(repo: Path) -> Path:
    return repo / ".git" / "tbhb-alpha"


def test_codex_hook_ident_fallbacks(repo: Path, tmp_path: Path) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    start = hook_data("codex-hook-sessionstart.json", repo, root)
    ident = codex.codex_hook_ident(ROOT_THREAD, start, {})
    assert ident.model == "openai:gpt-5.6-sol"
    # Without a usable stdin model the rollout's turn context is used.
    no_model = {k: v for k, v in start.items() if k != "model"}
    assert codex.codex_hook_ident(ROOT_THREAD, no_model, {}).model == (
        "openai:gpt-5.6-sol"
    )
    unusable = {**start, "model": "<synthetic>"}
    assert codex.codex_hook_ident(ROOT_THREAD, unusable, {}).model == (
        "openai:gpt-5.6-sol"
    )
    # Without a rollout only stdin's model is known.
    for gone in (None, str(tmp_path / "missing.jsonl"), 3):
        bare = {**start, "transcript_path": gone}
        assert codex.codex_hook_ident(ROOT_THREAD, bare, {}) == values.Ident(
            values.sha256_64(ROOT_THREAD), "codex", None, "openai:gpt-5.6-sol"
        )
    assert codex.codex_hook_ident(ROOT_THREAD, {}, {}) == values.Ident(
        values.sha256_64(ROOT_THREAD), "codex"
    )


def test_codex_apply_patch_writes_an_edit_time_receipt(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    expected = run_git(repo, "hash-object", "root.txt").strip()
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    run_hook(monkeypatch, data)
    # The file changes after the hook: the receipt keeps the edit-time blob.
    (repo / "root.txt").write_text("later\n", encoding="utf-8")
    (got,) = receipts_of(repo, values.sha256_64(ROOT_THREAD))
    assert got["path"] == "root.txt"
    assert got["blob"] == expected
    assert run_git(repo, "cat-file", "blob", expected) == "root\n"
    assert got["subagent"] is None
    assert got["harness"] == "codex:0.166.0"
    assert got["model"] == "openai:gpt-5.6-sol"
    assert got["effort"] == "high"
    assert got["surface"] == "exec"
    assert ROOT_THREAD not in json.dumps(got)


def test_codex_apply_patch_in_a_subagent_is_attributed_to_it(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, child = hook_rollouts(tmp_path / "codex", repo)
    (repo / "sub.txt").write_text("sub\n", encoding="utf-8")
    data = hook_data("codex-hook-posttooluse-subagent-apply-patch.json", repo, child)
    run_hook(monkeypatch, data)
    (got,) = receipts_of(repo, values.sha256_64(ROOT_THREAD))
    assert got["root"] == values.sha256_64(ROOT_THREAD)
    assert got["subagent"] == values.sha256_64(CHILD_THREAD)
    assert got["model"] == "openai:gpt-5.6-luna"
    assert got["effort"] == "low"
    assert got["surface"] is None


def test_codex_apply_patch_covers_every_patch_operation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = (
        "*** Begin Patch\n*** Add File: a.txt\n+a\n*** Update File: b.txt\n"
        "*** Move to: sub/c.txt\n@@\n-b\n+c\n*** Delete File: gone.txt\n"
        "*** End Patch"
    )
    for name in ("a.txt", "sub/c.txt"):
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text(name, encoding="utf-8")
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, None)
    data["tool_input"] = {"command": patch}
    run_hook(monkeypatch, data)
    got = {str(r["path"]): r for r in receipts_of(repo, values.sha256_64(ROOT_THREAD))}
    assert sorted(got) == ["a.txt", "b.txt", "gone.txt", "sub/c.txt"]
    assert got["a.txt"]["blob"] == run_git(repo, "hash-object", "a.txt").strip()
    assert got["b.txt"]["blob"] is None
    assert got["gone.txt"]["blob"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        {"hook_event_name": "PreToolUse"},
        {"hook_event_name": "UserPromptSubmit"},
        {"hook_event_name": "SessionEnd"},
        {"tool_name": "Bash"},
        {"tool_name": "apply_patch_x"},
        {"session_id": None},
        {"session_id": 7},
        {"session_id": ""},
    ],
)
def test_codex_ignores_other_events_and_tools(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mutate: Json
) -> None:
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, None, **mutate)
    run_hook(monkeypatch, data)
    assert not store_of(repo).exists()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[1, 2]",
        "{}",
        '{"hook_event_name": "PostToolUse", "tool_name": "apply_patch"}',
        (
            '{"session_id": "s", "hook_event_name": "PostToolUse",'
            ' "tool_name": "apply_patch", "tool_input": "oops", "tool_use_id": 3,'
            ' "cwd": 4, "transcript_path": 5}'
        ),
        (
            '{"session_id": "s", "hook_event_name": "PostToolUse",'
            ' "tool_name": "apply_patch", "tool_input": {"command": 9}}'
        ),
    ],
)
def test_codex_hook_fails_open_on_bad_stdin(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str,
) -> None:
    run_hook(monkeypatch, raw)
    assert capsys.readouterr().out == ""
    assert not list(store_of(repo).glob("edits/**/*.json"))


def test_codex_hook_outside_a_repository(
    plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (plain / "root.txt").write_text("root\n", encoding="utf-8")
    run_hook(
        monkeypatch, hook_data("codex-hook-posttooluse-apply-patch.json", plain, None)
    )
    run_hook(monkeypatch, hook_data("codex-hook-sessionstart.json", plain, None))
    assert [p.name for p in plain.iterdir()] == ["root.txt"]


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
    assert identity_commit.cmd_trailer(str(path), None, env) == 0
    session = values.sha256_64(ROOT_THREAD)
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
            f"subagent={values.sha256_64(CHILD_THREAD)};surface=exec"
        ),
    ]


def test_installed_codex_command_works_from_a_subdirectory(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "deep" / "dir").mkdir(parents=True)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    (handler,) = cast(
        "list[dict[str, str]]", install.codex_hooks()["PostToolUse"][0]["hooks"]
    )
    command = handler["command"]
    assert command == "tbhbdev alpha identity record codex # tbhbdev alpha install"
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    done = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        check=False,
        cwd=repo / "deep" / "dir",
        env=hook_env(),
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert [r["path"] for r in receipts_of(repo, values.sha256_64(ROOT_THREAD))] == [
        "root.txt"
    ]


@needs_prek
def test_e2e_codex_hooks_then_commit(wired: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    root, child = hook_rollouts(home, wired)
    start = hook_data("codex-hook-sessionstart.json", wired, root)
    record(wired, "codex", start)
    (wired / "root.txt").write_text("root\n", encoding="utf-8")
    record(
        wired,
        "codex",
        hook_data("codex-hook-posttooluse-apply-patch.json", wired, root),
    )
    (wired / "sub.txt").write_text("sub\n", encoding="utf-8")
    sub = hook_data("codex-hook-posttooluse-subagent-apply-patch.json", wired, child)
    record(wired, "codex", sub)
    run_git(wired, "add", "root.txt", "sub.txt")
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": ROOT_THREAD,
    }
    done = git_commit(wired, "feat: both", env)
    assert done.returncode == 0, done.stderr
    session = values.sha256_64(ROOT_THREAD)
    assert no_start(sorted(trailers(wired))) == [
        (
            "Generated-by: LLM effort=high;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-sol;session={session};surface=exec"
        ),
        (
            "Generated-by: LLM effort=low;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-luna;session={session};"
            f"subagent={values.sha256_64(CHILD_THREAD)};surface=exec"
        ),
    ]


def test_codex_hook_ident_takes_the_thread_from_the_hook_not_the_rollout(
    repo: Path, tmp_path: Path
) -> None:
    root, child = hook_rollouts(tmp_path / "codex", repo)
    other = write_rollout(tmp_path / "codex", OTHER_THREAD, [meta(id=OTHER_THREAD)])
    start = hook_data("codex-hook-sessionstart.json", repo, other)
    assert codex.codex_hook_ident(ROOT_THREAD, start, {}).subagent is None
    sub = hook_data("codex-hook-subagentstart.json", repo, other)
    got = codex.codex_hook_ident(ROOT_THREAD, sub, {})
    assert got.subagent == values.sha256_64(CHILD_THREAD)
    assert (root, child) != (other, other)


def test_codex_hooks_use_the_hook_cwd_not_the_process_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    run_hook(monkeypatch, hook_data("codex-hook-sessionstart.json", repo, root))
    run_hook(
        monkeypatch, hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    )
    assert [r["path"] for r in receipts_of(repo, values.sha256_64(ROOT_THREAD))] == [
        "root.txt"
    ]
    assert (store_of(repo) / "sessions").is_dir()
