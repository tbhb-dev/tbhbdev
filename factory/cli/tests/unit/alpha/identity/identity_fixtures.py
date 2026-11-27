# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Isolated repositories and harness fixtures for identity hook tests."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast
lazy from collections.abc import Callable

import pytest
import yaml
from fake_tool import pythonpath
from unit.alpha.identity.identity_examples import (
    EARLIER,
    EARLY,
    LAUNCH,
    OPUS,
    ROOT,
    SESSION_HASH,
    SONNET,
    Json,
    document,
    make_receipt,
)

from tbhbdev.alpha import repo as git_repo
from tbhbdev.alpha.identity import (
    dispatcher,
    models,
    receipts,
    records,
    sessions,
    storage,
)
from tbhbdev.alpha.identity.codex import rollout as codex_rollout
from tbhbdev.alpha.identity.commit import policy as commit_policy

SRC = Path(dispatcher.__file__).parents[3]


ORIGIN = "git@github.com:tbhb-dev/example.git"


HUMAN_COMMIT_ENV = {
    f"GIT_{role}_{field}": value
    for role in ("AUTHOR", "COMMITTER")
    for field, value in (("NAME", "Human"), ("EMAIL", "h@example.invalid"))
}


AGENT_VARS = (
    "GIT_",
    "PRE_COMMIT",
    "PREK",
    "CLAUDE",
    "CODEX",
    "ANTIGRAVITY",
    "AI_AGENT",
    "TBHB_GH_",
)


@pytest.fixture(scope="session")
def fake_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A `tbhbdev` on PATH that runs this source tree, as the hooks expect."""
    path = tmp_path_factory.mktemp("bin")
    tool = path / "tbhbdev"
    tool.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(pythonpath(SRC))} "
        f'exec {shlex.quote(sys.executable)} -m tbhbdev "$@"\n',
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return path


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_bin: Path) -> Path:
    """No injected GIT_* or agent variables, no user git config, a temp HOME."""
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(AGENT_VARS)]))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A hook with no cwd reads and writes the cwd's repository: keep it out of ours.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    # The agent identity, which commit-msg requires of a commit in a harness.
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "tbhb.dev Agent")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "agent@tonyburns.net")
    return tmp_path


def run_git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=hook_env(),
    )
    return done.stdout


def init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    run_git(path, "config", "commit.gpgsign", "false")
    run_git(path, "remote", "add", "origin", ORIGIN)
    return path.resolve()


@pytest.fixture
def repo(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = init_repo(isolated / "repo")
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def plain_checkout(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory outside any repository, as the working directory."""
    path = isolated / "plain"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def stage(repo: Path, name: str, text: str | None = None) -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name + "\n" if text is None else text, encoding="utf-8")
    run_git(repo, "add", "--", name)


def stdin_fixture(name: str, **overrides: object) -> Json:
    return {**loaded(DATA / name), **overrides}


def feed(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def no_start[T](value: T) -> T:
    """``value`` without any ``started_at``, which is the time the test ran."""
    if isinstance(value, dict):
        items = cast("dict[str, object]", value).items()
        return cast("T", {k: v for k, v in items if k != "started_at"})
    if isinstance(value, str):
        return cast("T", re.sub(r";started_at=[^;\s]+", "", value))
    return cast("T", type(value)(map(no_start, value)))  # type: ignore[call-arg]


def add_worktree(repo: Path, tree: Path) -> Path:
    """A linked worktree of `repo` at `tree`, on a new branch named after it."""
    done = git_commit(repo, "base", HUMAN_COMMIT_ENV, "--allow-empty")
    assert done.returncode == 0, done.stderr
    run_git(repo, "worktree", "add", "-q", "-b", tree.name, str(tree))
    return tree.resolve()


def write_transcript(path: Path, *records: Json) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def pending(repo: Path) -> list[Json]:
    cmds = repo / ".git" / "tbhb-alpha" / "cmds"
    return [loaded(p) for p in sorted(cmds.rglob("*.json"))]


def returning[T](value: T) -> Callable[..., T]:
    """A stand-in that ignores its arguments and returns ``value``."""

    def fake(*_args: object, **_kwargs: object) -> T:
        return value

    return fake


CLAUDE_HOOK_ENV = {
    "CLAUDE_CODE_EXECPATH": "/x/2.1.316",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
}


AGENT = records.sha256_64("agent-raw")


def claude_data(
    repo: Path, name: str = "claude-pretooluse-bash-main.json", **kw: object
) -> Json:
    (repo / "session-raw.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n", encoding="utf-8"
    )
    return stdin_fixture(
        name, cwd=str(repo), transcript_path=str(repo / "session-raw.jsonl"), **kw
    )


def receipts_of(repo: Path, root: str) -> list[Json]:
    store = storage.Repo(
        repo, repo / ".git" / "tbhb-alpha", repo / ".git" / "tbhb-alpha"
    )
    return sorted(
        (r for _, r in receipts.load_edits(store, root)), key=lambda r: str(r["path"])
    )


ROOT_THREAD = "01a19ebf-adec-7210-8ba8-550921c2b924"


CHILD_THREAD = "01a19ebf-e15a-7612-a93f-033e919906de"


def write_rollout(home: Path, thread: str, lines: list[object]) -> Path:
    folder = home / "sessions" / "2026" / "11" / "02"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-02T19-12-17-{thread}.jsonl"
    text = ""
    for line in lines:
        text += (line if isinstance(line, str) else json.dumps(line)) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def turn(model: str | None, effort: str | None) -> Json:
    return {"type": "turn_context", "payload": {"model": model, "effort": effort}}


def meta(**payload: object) -> Json:
    return {"type": "session_meta", "payload": payload}


def patch_paths_of(path: Path, offset: int = 0, skip: int = 0) -> list[str]:
    return codex_rollout.rollout_patch_edits(path, offset, skip)[0]


CONV = "conv-raw"


CLAUDE_COMMIT_ENV = {
    "CLAUDE_CODE_SESSION_ID": "session-raw",
    "CLAUDE_CODE_EXECPATH": "/x/2.1.316",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_EFFORT": "low",
}


def seed_claude(repo: Path) -> None:
    sessions.write_record(
        SESSION_HASH,
        {"version": "2.1.316", "model": "claude-opus-5-5", "effort": "high"},
        repo,
    )


def msg_file(tmp_path: Path, text: str = "subject\n\nbody\n") -> Path:
    path = tmp_path / "MSG"
    path.write_text(text, encoding="utf-8")
    return path


def commit(repo: Path, message: str = "m") -> None:
    run_git(repo, "commit", "-q", "--no-verify", "-m", message)


def edits_dir(repo: Path, root: str = ROOT) -> Path:
    return repo / ".git" / "tbhb-alpha" / "edits" / records.hx(root)


def put_receipt(repo: Path, name: str, receipt: Json, root: str = ROOT) -> Path:
    path = edits_dir(repo, root) / name
    storage.write_json(path, receipt)
    return path


def claims_of(repo: Path) -> dict[str, Json]:
    folder = repo / ".git" / "tbhb-alpha" / "claims"
    return {p.stem: loaded(p) for p in sorted(folder.glob("*.json"))}


NO_SESSION_MESSAGE = (
    "this commit claims the agent identity but no agent session is attached. "
    "Agents: commit from the harness's own host, where the session's hooks run. "
    "Humans: commit as yourself; the agent identity is reserved for agent sessions."
)


# A prek config with only the agent-identity hook, as a tbhb-dev repository
# that stamps its docs would carry.
PREK_CONFIG = {
    "repos": [
        {
            "repo": "local",
            "hooks": [
                {
                    "id": "agent-identity-stamp-docs",
                    "name": "agent identity stamp docs",
                    "language": "system",
                    "entry": "tbhbdev alpha identity stamp-docs",
                    "stages": ["pre-commit"],
                    "types": ["markdown"],
                    "pass_filenames": False,
                }
            ],
        }
    ]
}


needs_prek = pytest.mark.skipif(
    shutil.which("prek") is None and shutil.which("mise") is None,
    reason="prek (or mise to run it) is required",
)


@pytest.fixture
def wired(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tbhb-dev repository with `tbhbdev alpha install`'s git hooks."""
    path = init_repo(isolated / "wired")
    (path / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(PREK_CONFIG), encoding="utf-8"
    )
    stage(path, "base.txt")
    run_git(path, "add", ".pre-commit-config.yaml")
    run_git(path, "commit", "-q", "-m", "init")
    monkeypatch.chdir(path)
    done = subprocess.run(
        ["tbhbdev", "alpha", "install", "--harness", "claude"],
        capture_output=True,
        text=True,
        check=False,
        env=hook_env(),
    )
    assert done.returncode == 0, done.stderr
    return path


def hook_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(AGENT_VARS[1:])}
    # Mutmut's marker would make the child look up mutmut's config from the
    # temp repo; the child runs the module unmutated.
    env.pop("MUTANT_UNDER_TEST", None)
    # An inherited model would stamp every child as an agent run.
    env.pop(commit_policy.RUN_MODEL_ENV, None)
    env.update(extra or {})
    return env


def record(
    repo: Path, harness: str, data: Json, extra: dict[str, str] | None = None
) -> None:
    done = subprocess.run(
        ["tbhbdev", "alpha", "identity", "record", harness],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        check=False,
        cwd=repo,
        env=hook_env(extra),
    )
    assert (done.returncode, done.stderr) == (0, "")


def git_commit(
    repo: Path,
    message: str,
    extra: dict[str, str] | None = None,
    *flags: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "commit", "-q", "-m", message, *flags],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd or repo,
        env=hook_env(extra),
    )


def last_message(repo: Path) -> str:
    return run_git(repo, "log", "-1", "--format=%B").rstrip("\n")


def trailers(repo: Path) -> list[str]:
    return [
        x
        for x in last_message(repo).splitlines()
        if x.startswith(("Generated-by", "Assisted-by"))
    ]


CLAUDE_E2E = {
    "CLAUDE_CODE_SESSION_ID": "session-raw",
    "CLAUDE_CODE_EXECPATH": "/x/2.1.316",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
}


def claude_turn(repo: Path) -> None:
    """A main-agent turn: its transcript has an opus reply, and a hook saw it."""
    (repo / "t.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n", encoding="utf-8"
    )
    data = stdin_fixture(
        "claude-pretooluse-bash-main.json",
        cwd=str(repo),
        transcript_path=str(repo / "t.jsonl"),
    )
    record(repo, "claude", data, CLAUDE_E2E)


def claude_write(
    repo: Path, name: str, content: str | None = None, **kw: object
) -> None:
    (repo / "t.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n", encoding="utf-8"
    )
    if "agent_id" in kw:
        write_transcript(
            repo / "t" / "subagents" / f"agent-{kw['agent_id']}.jsonl",
            {"message": {"model": "claude-haiku-5-5"}},
        )
    data = stdin_fixture(
        "claude-posttooluse-write.json",
        cwd=str(repo),
        transcript_path=str(repo / "t.jsonl"),
        tool_input={"file_path": name},
        **kw,
    )
    (repo / name).write_text(
        f"{name}\n" if content is None else content, encoding="utf-8"
    )
    record(repo, "claude", data, CLAUDE_E2E)


NOREPLY = "tbhb.dev Agent <334376894+tbhb-agent@users.noreply.github.com>"


SUB_E2E = records.sha256_64("agent-raw")


def generated_subagents(repo: Path) -> list[str | None]:
    """The ``subagent=`` of each Generated-by trailer on HEAD (None: the root)."""
    return [
        (m[1] if (m := re.search(r"subagent=(\S+?)(;|$)", line)) else None)
        for line in trailers(repo)
        if line.startswith("Generated-by")
    ]


def spy_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every directory that is globbed or listed."""
    seen: list[Path] = []
    real_glob, real_iterdir = Path.glob, Path.iterdir

    def glob(self: Path, pattern: str) -> object:
        seen.append(self)
        return real_glob(self, pattern)

    def iterdir(self: Path) -> object:
        seen.append(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    return seen


def edit_by(who: models.Ident, path: str, ts: float, repo: Path | None = None) -> None:
    info = storage.repo_info(repo)
    assert info is not None
    name = f"{path.replace('/', '_')}-{who.root[-4:]}-{who.subagent}-{ts}.json"
    receipt = make_receipt(path, who, None, ts)
    storage.write_json(info.state / "edits" / records.hx(who.root) / name, receipt)


def committed_as(
    who: models.Ident, root: models.Ident | None = None
) -> models.Committer:
    return models.Committer(models.CLAUDE, who, root or who)


def stage_doc(repo: Path, rel: str, text: str | bytes | None = None) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    content = document() if text is None else text
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8", newline="")
    else:
        target.write_bytes(content)
    run_git(repo, "add", "--", rel)


def staged_text(repo: Path, rel: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f":{rel}"],
        check=True,
        capture_output=True,
        env=hook_env(),
    ).stdout


def hook_data(name: str, repo: Path, rollout: Path | None, **kw: object) -> Json:
    data = stdin_fixture(name, cwd=str(repo), **kw)
    if rollout:
        data["transcript_path"] = str(rollout)
    return data


def hook_rollouts(home: Path, repo: Path) -> tuple[Path, Path]:
    """(root rollout, child rollout) shaped like Codex 0.166.0's."""
    root = write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(
                id=ROOT_THREAD,
                session_id=ROOT_THREAD,
                cwd=str(repo),
                source="exec",
                cli_version="0.166.0",
            ),
            turn("gpt-5.6-sol", "high"),
        ],
    )
    child = write_rollout(
        home,
        CHILD_THREAD,
        [
            child_meta(repo, cli_version="0.166.0"),
            turn("gpt-5.6-terra", "low"),
        ],
    )
    return root, child


def run_hook(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    feed(monkeypatch, data)
    assert dispatcher.cmd_record("codex", {}) == 0


def reply(model: str, timestamp: str | None = EARLIER) -> Json:
    entry: Json = {"type": "assistant", "message": {"model": model}}
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return entry


def switch_data(repo: Path, to_model: object = SONNET) -> Json:
    return claude_data(
        repo,
        hook_event_name="PostModelSwitch",
        tool_name=None,
        tool_input=None,
        from_model=OPUS,
        to_model=to_model,
    )


def session_file(store: Path) -> Path:
    return store / "sessions" / f"{records.hx(SESSION_HASH)}.json"


SESSION_RAW = records.sha256_64("session-raw")


NOW = 1_794_614_400.0


@pytest.fixture
def launch_file(repo: Path) -> Path:
    """The launch file a launcher writes before its harness starts."""
    path = repo / ".git" / "tbhb-alpha" / "launch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"launch": LAUNCH, "started_at": EARLY}))
    return path


# Resolve the fixtures from the test root in both source and Bazel runfiles.
DATA = Path(__file__).parents[3] / "testdata" / "alpha"


def loaded(path: Path) -> Json:
    return cast("Json", json.loads(path.read_text(encoding="utf-8")))


def observe_git(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[str, ...], Path | None]]:
    """Collect Git calls while allowing them to run."""
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    real = git_repo.git

    def spy(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        calls.append((args, cwd))
        return real(*args, cwd=cwd)

    monkeypatch.setattr(git_repo, "git", spy)
    return calls


def child_meta(cwd: Path, **payload: object) -> Json:
    """Metadata for a Codex child thread belonging to the example root."""
    return meta(
        id=CHILD_THREAD,
        session_id=ROOT_THREAD,
        cwd=str(cwd),
        source={"subagent": 1},
        **payload,
    )
