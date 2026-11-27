# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Shared helpers for identity hook tests."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import jsonschema
import pytest
import yaml

from tbhbdev.alpha.identity import (
    claude,
    codex_rollout,
    dispatcher,
    edits,
    frontmatter,
    sessions,
    storage,
    values,
)
from tbhbdev.alpha.identity import record as identity_record
from tbhbdev.alpha.identity import trailers as identity_trailers

if TYPE_CHECKING:
    from collections.abc import Callable

# mutmut runs the tests from a copy of the source tree, so fixtures resolve
# against this file rather than the working directory.
DATA = Path(__file__).parents[3] / "testdata" / "alpha"

SRC = Path(dispatcher.__file__).parents[3]

ORIGIN = "git@github.com:tbhb-dev/example.git"

HUMAN_COMMIT_ENV = {
    f"GIT_{role}_{field}": value
    for role in ("AUTHOR", "COMMITTER")
    for field, value in (("NAME", "Human"), ("EMAIL", "h@example.invalid"))
}

H = "sha-256-64:"

ROOT = H + "a" * 16

CHILD = H + "c" * 16

OTHER = H + "d" * 16

AGENT_VARS = (
    "GIT_",
    "PRE_COMMIT",
    "PREK",
    "CLAUDE",
    "CODEX",
    "ANTIGRAVITY",
    "ASCM_",
    "AI_AGENT",
    "TBHB_GH_",
)


type Json = dict[str, object]


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


def stage(repo: Path, name: str, text: str | None = None) -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name + "\n" if text is None else text, encoding="utf-8")
    run_git(repo, "add", "--", name)


def loaded(path: Path) -> Json:
    return cast("Json", json.loads(path.read_text(encoding="utf-8")))


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


def make_ident(
    sub: str | None = None,
    model: str | None = "anthropic:m",
    root: str = ROOT,
    effort: str | None = None,
    surface: str | None = None,
) -> values.Ident:
    return values.Ident(root, "claude-code", sub, model, effort, surface)


def make_receipt(
    path: str,
    who: values.Ident,
    blob: str | None = None,
    ts: float = 1,
) -> Json:
    return {
        "root": who.root,
        "subagent": who.subagent,
        "harness": who.harness,
        "model": who.model,
        "effort": who.effort,
        "surface": who.surface,
        "path": path,
        "blob": blob,
        "ts": ts,
        "consumed": False,
    }


def committer_of(
    who: values.Ident, root: values.Ident | None = None
) -> values.Committer:
    return values.Committer(values.CLAUDE, who, root or who)


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


class FakeRun:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


CLAUDE_HOOK_ENV = {
    "CLAUDE_CODE_EXECPATH": "/x/2.1.316",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
}

SESSION_HASH = values.sha256_64("session-raw")

AGENT = values.sha256_64("agent-raw")


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
        (r for _, r in edits.load_edits(store, root)), key=lambda r: str(r["path"])
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


def patch_call(*paths: str) -> Json:
    body = "*** Begin Patch\n" + "".join(f"*** Add File: {p}\n+x\n" for p in paths)
    patch = json.dumps(body + "*** End Patch")
    code = f"const patch = {patch};\nawait tools.apply_patch(patch);"
    payload: Json = {"type": "custom_tool_call", "name": "exec", "input": code}
    return {"type": "response_item", "payload": payload}


CONV = "conv-raw"


def agy_post(**overrides: object) -> Json:
    return stdin_fixture("agy-posttooluse-write.json", **overrides)


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


GEN_ROOT = ("Generated-by", f"LLM harness=claude-code;session={ROOT}")

GEN_SUB = ("Generated-by", f"LLM harness=claude-code;session={ROOT};subagent={CHILD}")

ASSIST_OTHER = ("Assisted-by", f"LLM harness=codex;session={OTHER}")


def commit(repo: Path, message: str = "m") -> None:
    run_git(repo, "commit", "-q", "--no-verify", "-m", message)


def edits_dir(repo: Path, root: str = ROOT) -> Path:
    return repo / ".git" / "tbhb-alpha" / "edits" / values.hx(root)


def put_receipt(repo: Path, name: str, receipt: Json, root: str = ROOT) -> Path:
    path = edits_dir(repo, root) / name
    storage.write_json(path, receipt)
    return path


def claims_of(repo: Path) -> dict[str, Json]:
    folder = repo / ".git" / "tbhb-alpha" / "claims"
    return {p.stem: loaded(p) for p in sorted(folder.glob("*.json"))}


GOOD = f"harness=claude-code:2.1;model=anthropic:m;session={ROOT}"

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


def hook_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(AGENT_VARS[1:])}
    # Mutmut's marker would make the child look up mutmut's config from the
    # temp repo; the child runs the module unmutated.
    env.pop("MUTANT_UNDER_TEST", None)
    # An inherited model would stamp every child as an agent run.
    env.pop(values.RUN_MODEL_ENV, None)
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

SIGNOFF_BASE = (
    "agent commits carry no Signed-off-by or Co-authored-by; the operator signs off"
)

SUB_E2E = values.sha256_64("agent-raw")


def generated_subagents(repo: Path) -> list[str | None]:
    """The ``subagent=`` of each Generated-by trailer on HEAD (None: the root)."""
    return [
        (m[1] if (m := re.search(r"subagent=(\S+?)(;|$)", line)) else None)
        for line in trailers(repo)
        if line.startswith("Generated-by")
    ]


COG_LIKE = """#!/bin/sh
echo ran >> cog.log
grep -qE '^(feat|fix|chore): ' "$1" && exit 0
echo "cog: not a conventional commit" >&2
exit 1
"""


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


def fork_lines(replayed: int) -> list[object]:
    """A synthetic forked rollout: ``replayed`` inherited lines, then its own."""
    inherited: list[object] = [
        meta(
            id=CHILD_THREAD,
            session_id=ROOT_THREAD,
            forked_from_id=ROOT_THREAD,
            **{START: replayed},
        ),
        patch_call("parent.md"),
        *[{"type": "response_item", "payload": {"type": "message"}}] * (replayed - 2),
    ]
    own: list[object] = [
        {"type": "event_msg", "payload": {"type": "thread_settings_applied"}},
        patch_call("child.md"),
    ]
    return [*inherited, *own]


START = "subagent_history_start_ordinal"

SCHEMA = loaded(DATA / "frontmatter.schema.json")

VALIDATOR = jsonschema.Draft202012Validator(
    SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)

HEAD = "type: Reference\ntitle: T\ndescription: D.\ntags:\n  - a-b\nstatus: draft\n"

AT1 = "2026-11-02T10:00:00Z"

AT2 = "2026-11-02T11:00:00Z"


type Entry = dict[str, str]


def entry(**fields: str) -> Entry:
    base = {
        "role": "generated",
        "harness": "claude-code:2.1.316",
        "model": "anthropic:claude-opus-5-5",
        "at": AT1,
    }
    return {**base, **fields}


def document(*lines: str, head: str = HEAD, tail: str = "", body: str = "# T\n") -> str:
    block = "".join(f"{line}\n" for line in lines)
    return f"---\n{head}{block}{tail}---\n{body}"


def front_of(text: str) -> Json:
    """The parsed frontmatter, checked against the repo's schema."""
    data = yaml.safe_load(text.split("---\n")[1])
    VALIDATOR.validate(data)
    return cast("Json", data)


def listing(*entries: Entry, indent: int = 2) -> list[str]:
    return [line for e in entries for line in frontmatter.render_entry(e, indent)]


def edit_by(who: values.Ident, path: str, ts: float, repo: Path | None = None) -> None:
    info = storage.repo_info(repo)
    assert info is not None
    name = f"{path.replace('/', '_')}-{who.root[-4:]}-{who.subagent}-{ts}.json"
    receipt = make_receipt(path, who, None, ts)
    storage.write_json(info.state / "edits" / values.hx(who.root) / name, receipt)


MAIN = values.Ident(
    SESSION_HASH, "claude-code", None, "anthropic:claude-opus-5-5", "high", "cli"
)

STAMP_AT = identity_trailers.utc(1_800_000_000)


def committed_as(
    who: values.Ident, root: values.Ident | None = None
) -> values.Committer:
    return values.Committer(values.CLAUDE, who, root or who)


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
            meta(
                id=CHILD_THREAD,
                session_id=ROOT_THREAD,
                cwd=str(repo),
                source={"subagent": 1},
                cli_version="0.166.0",
            ),
            turn("gpt-5.6-terra", "low"),
        ],
    )
    return root, child


def run_hook(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    feed(monkeypatch, data)
    assert identity_record.cmd_record("codex", {}) == 0


OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"

EARLIER = "2026-11-06T10:00:00.000Z"

EARLIER_TS = 1_793_959_200.0  # EARLIER in seconds


def reply(model: str, timestamp: str | None = EARLIER) -> Json:
    entry: Json = {"type": "assistant", "message": {"model": model}}
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return entry


def session_file(store: Path) -> Path:
    return store / "sessions" / f"{values.hx(SESSION_HASH)}.json"


SESSION_RAW = values.sha256_64("session-raw")

LAUNCH = H + "0a1b2c3d4e5f6071"

EARLY, LATE = "2026-11-14T00:02:41Z", "2026-11-14T00:09:00Z"

NOW = 1_794_614_400.0

NOW_UTC = "2026-11-14T00:00:00Z"


def claude_event(repo: Path, session: str, agent: str | None = None) -> None:
    data: Json = {"session_id": session, "hook_event_name": "SessionStart"}
    claude.handle_claude(
        {**data, "cwd": str(repo), **({"agent_id": agent} if agent else {})}, {}
    )


def line_of(start: str | None = None, extra: str = "", who: str = ROOT) -> str:
    stamp = f"started_at={start};" if start else ""
    base = f"LLM {extra}harness=claude-code;model=anthropic:m"
    return f"{base};session={who};{stamp}surface=cli"
