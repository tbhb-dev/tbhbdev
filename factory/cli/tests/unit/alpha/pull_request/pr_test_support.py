# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:ignore Ftopic

"""Fake worktrees and tools for pull request command tests."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from fake_tool import PR_ORIGIN as ORIGIN
from fake_tool import (
    PRIVATE_CONFIG,
    RULES_BEYOND_ONE,
    link_git,
    write_fake,
    write_private_config,
    write_test_hooks,
)
from unit.alpha.pull_request.pr_draft_samples import BASE

from tbhbdev.alpha import size_gate, tool_pins
from tbhbdev.alpha.identity import records, sessions
from tbhbdev.alpha.identity.claude import ancestry
from tbhbdev.alpha.pull_request import baseline as pr_baseline
from tbhbdev.alpha.pull_request import command as pr_command
from tbhbdev.alpha.pull_request import prose as pr_prose
from tbhbdev.alpha.pull_request import repository as pr_repository
from tbhbdev.alpha.pull_request import tools as pr_tools

# Tests can run from the project or a copy made by mutmut.
REAL = Path(__file__).resolve().parents[4]
WORKSPACE = next(parent.parent for parent in REAL.parents if parent.name == "factory")
SRC = Path(pr_tools.__file__).parents[3]
DATA = Path(__file__).parents[3] / "testdata" / "alpha" / "pull_request"
FIXTURE = DATA / "passing-draft.md"
TEMPLATE = WORKSPACE / ".github" / "PULL_REQUEST_TEMPLATE.md"
FAKE = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.basename(__file__).removesuffix(".py")
spec = json.load(open(os.path.join(here, name + ".spec")))
called = " ".join(sys.argv[1:])
if called == "--version" and spec["version"] is not None:
    print(f"{{name}} version {{spec['version']}}")
    sys.exit(0)
cases = spec.get("cases", {{}})
if called == "pr view --json number,state,url":
    missing = {{"rc": 1, "stdout": "", "stderr": "No Pull Requests Found"}}
    spec = cases.get(called, missing)
elif called.startswith("api --hostname github.com --paginate --slurp "):
    spec = cases.get(called, {{
        "rc": 0, "stdout": '[[{{"commit": {{"message": "plain"}}}}]]', "stderr": ""
    }})
else:
    spec = next((v for k, v in cases.items() if called.startswith(k)), spec)
draft = ".github/PULL_REQUEST_DRAFT.md"
record = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "stdin_tty": sys.stdin.isatty(),
    "config": (
        open(sys.argv[sys.argv.index("--config") + 1]).read()
        if "--config" in sys.argv
        else None
    ),
    "draft": open(draft).read() if os.path.exists(draft) else None,
    "links": {{n: os.readlink(n) for n in os.listdir(".") if os.path.islink(n)}},
}}
# The visibility read has its own log, so it never shifts the other gh calls.
repo = ".repo" if called.startswith("repo view") else ""
with open(os.path.join(here, name + repo + ".log"), "a") as log:
    log.write(json.dumps(record) + "\\n")
sys.stdout.write(spec["stdout"])
sys.stderr.write(spec["stderr"])
sys.exit(spec["rc"])
"""


TOOL_VERSIONS = {name: tool_pins.pinned_version(name) for name in tool_pins.PINNED}
TARGET = "tbhb-dev/public-target"
PR_COMMITS_CALL = ["api", "--hostname", "github.com", "--paginate", "--slurp"]
BRANCH_LOOKUP = (
    "api --hostname github.com repos/tbhb-dev/example/branches/agent%2Ftopic"
)


def pr_page(repo: str, number: int = 7) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def view_case(url: object, body: object = "") -> dict[str, object]:
    """The fake gh answer to ``gh pr view N --json body,url``; no ``url`` omits it."""
    data: dict[str, object] = {"body": body}
    if url is not None:
        data["url"] = url
    return {"rc": 0, "stdout": json.dumps(data), "stderr": ""}


def base_case(name: object, visibility: object, rc: int = 0) -> dict[str, object]:
    """The fake gh answer to ``gh repo view --json nameWithOwner,visibility``."""
    data = {"nameWithOwner": name, "visibility": visibility}
    return {
        "rc": rc,
        "stdout": json.dumps(data) if rc == 0 else "",
        "stderr": "boom" if rc else "",
    }


@dataclass
class World:
    """A temp repository, a bin directory of fake tools, and an isolated PATH."""

    root: Path
    bin: Path

    def fake(
        self,
        name: str,
        *,
        rc: int = 0,
        stdout: str = "",
        stderr: str = "",
        cases: dict[str, dict[str, object]] | None = None,
    ) -> None:
        """A fake tool; ``cases`` answers the calls whose words start with a key."""
        write_fake(self.bin / name, FAKE.format())
        spec = {
            "rc": rc,
            "stdout": stdout,
            "stderr": stderr,
            "cases": cases or {},
            "version": TOOL_VERSIONS.get(name),
        }
        (self.bin / f"{name}.spec").write_text(json.dumps(spec))

    def calls(self, name: str) -> list[dict[str, object]]:
        log = self.bin / f"{name}.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(self, message: str) -> None:
        self.git("commit", "--allow-empty", "-q", "-m", message)


# The world fixture replaces these checks; tests can put them back.
REAL_SIZE_COUNTS = size_gate.size_counts
REAL_TARGET_IS_ORIGIN = pr_repository.target_is_origin
REAL_GIT = pr_tools.git
REAL_SCC = shutil.which("scc")


@pytest.fixture(autouse=True)
def host_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the workspace baseline with a synthetic home and Git identity."""
    monkeypatch.chdir(WORKSPACE)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "gitconfig"
    config.write_text("[user]\nname = Example\nemail = example@example.test\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def copy_pr_baseline(root: Path) -> None:
    (root / pr_baseline.SCHEMA).parent.mkdir(parents=True)
    shutil.copy(WORKSPACE / pr_baseline.SCHEMA, root / pr_baseline.SCHEMA)
    shutil.copy(WORKSPACE / pr_baseline.SECTIONS, root / pr_baseline.SECTIONS)


def world_files(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """Give each test its own draft files, fake tools, logs, and home."""
    monkeypatch.setattr(
        size_gate,
        "size_counts",
        Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None)),
    )
    monkeypatch.setattr(pr_repository, "target_is_origin", Mock(return_value=True))
    tools_dir = tmp_path / "bin"
    tools_dir.mkdir()
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    prefixes = ("CLAUDE", "CODEX", "ANTIGRAVITY", "GIT_", "ASCM_")
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(prefixes)]))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "T")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")
    copy_pr_baseline(root)
    for name in pr_prose.CONFIGS:
        (root / name).write_text("")
    (root / ".gitleaks.toml").write_text("")
    write_private_config(tmp_path, PRIVATE_CONFIG + RULES_BEYOND_ONE)
    made = World(root, tools_dir)
    made.fake("cog")
    made.fake("bazelisk")
    made.fake("scc")
    made.fake("vale", stdout="{}")
    made.fake("cspell")
    made.fake("rumdl")
    made.fake("gitleaks", stdout="[]")
    made.fake(
        "gh",
        stdout="https://github.com/tbhb-dev/tbhbdev/pull/1\n",
        cases={
            "pr view": view_case(pr_page(TARGET)),
            f"repo view {TARGET}": {"rc": 0, "stdout": "PUBLIC\\n", "stderr": ""},
            "repo view --json": base_case(TARGET, "PUBLIC"),
        },
    )
    return made


@pytest.fixture
def world(
    pr_checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> World:
    """Copy Git metadata for tests that exercise real refs or worktrees."""
    made = world_files(pr_checkout, tmp_path, monkeypatch)
    git_dir = tmp_path / "git_dir"
    link_git(git_dir)
    monkeypatch.setenv("PATH", f"{made.bin}{os.pathsep}{git_dir}")
    write_test_hooks(made.root / ".git/hooks")

    def offline_git(*argv: str, cwd: Path | None = None) -> tuple[int, str]:
        # Preserve the seeded tracking refs without contacting the fake origin.
        if argv[:1] == ("fetch",):
            return 0, ""
        if argv[:1] == ("ls-remote",) and REAL_GIT(
            "remote", "get-url", "origin", cwd=cwd
        ) == (0, ORIGIN):
            return 1, ""
        return REAL_GIT(*argv, cwd=cwd)

    monkeypatch.setattr(pr_tools, "git", offline_git)
    return made


@pytest.fixture
def draft_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """Run draft checks with fake tools and fixed answers for Git lookups."""
    made = world_files(tmp_path / "repo", tmp_path, monkeypatch)
    git_dir = tmp_path / "git_dir"
    link_git(git_dir)
    monkeypatch.setenv("PATH", f"{made.bin}{os.pathsep}{git_dir}")
    monkeypatch.chdir(made.root)
    base = "refs/remotes/origin/main"
    fetch_options = ("--quiet", "--no-tags", "--no-write-fetch-head", "--refmap=")
    answers: dict[tuple[str, ...], tuple[int, str]] = {
        ("rev-parse", "--show-toplevel"): (0, str(made.root.resolve())),
        ("ls-remote", "--symref", "origin", "HEAD"): (128, ""),
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (0, base),
        ("fetch", *fetch_options, "origin", f"+refs/heads/main:{base}"): (0, ""),
        ("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"): (0, ""),
        ("branch", "--show-current"): (0, "agent/topic"),
    }

    def answer_git(*argv: str, **_kwargs: Path | None) -> tuple[int, str]:
        return answers[argv]

    monkeypatch.setattr(pr_tools, "git", Mock(side_effect=answer_git))
    return made


AGENT = {
    "CLAUDE_CODE_SESSION_ID": "raw-session",
    "CLAUDE_CODE_EXECPATH": "/x/2.1.0",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_EFFORT": "high",
}


POSTING = (
    "LLM effort=high;harness=claude-code:2.1.0;model=anthropic:claude-opus-5-5;"
    f"session={records.sha256_64('raw-session')};surface=cli"
)


ROOT = records.sha256_64("raw-session")
OPENER = (
    "Opened-by: LLM harness=claude-code:2.1.0;model=anthropic:claude-opus-5-5;"
    f"session={ROOT};surface=cli"
)


def ledgers(world: World) -> Path:
    """The directory of opened ledgers in the world's user store."""
    return world.root.parent / "state" / "tbhbdev" / "tbhb-alpha" / "opened"


def seed_session(world: World, model: str | None = "claude-opus-5-5") -> None:
    """The posting session's record, with a transcript that has one reply."""
    transcript = world.root.parent / "session.jsonl"
    message = {"model": model} if model else {"content": "x"}
    transcript.write_text(json.dumps({"message": message}) + "\n")
    sessions.write_record(ROOT, {"transcript": str(transcript)}, world.root)


@pytest.fixture
def agent(world: World, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(ancestry, "ancestry", lambda: ["sh -c eval 'open'"])
    seed_session(world)
    return AGENT


def run_main(*argv: str, env: dict[str, str] | None = None) -> int:
    return pr_command.main(list(argv), env or {})


def write_draft(world: World, text: str = BASE) -> Path:
    path = world.root / ".github" / "PULL_REQUEST_DRAFT.md"
    path.write_text(text)
    os.chdir(world.root)  # the default draft path is relative to the cwd
    return path


def gh_list(items: list[dict[str, object]], rc: int = 0) -> dict[str, object]:
    return {"rc": rc, "stdout": json.dumps(items), "stderr": ""}


READY_VIEW = "pr view 45 --repo github.com/tbhb-dev/example --json author --jq"
READY_ME = "api --hostname github.com user --jq .login"


def ready_cases(
    author: str = "agent", me: str = "agent"
) -> dict[str, dict[str, object]]:
    return {
        "repo view": branch_cases()["repo view"],
        READY_VIEW: {"rc": 0, "stdout": f"{author}\n", "stderr": ""},
        READY_ME: {"rc": 0, "stdout": f"{me}\n", "stderr": ""},
    }


def gh_argvs(world: World) -> list[list[str]]:
    calls = world.calls("gh.repo") + world.calls("gh")
    return [cast("list[str]", c["argv"]) for c in calls]


def branch_cases(**changes: object) -> dict[str, dict[str, object]]:
    data: dict[str, object] = {
        "state": "CLOSED",
        "headRefName": "agent/topic",
        "isCrossRepository": False,
    }
    data.update(changes)
    return {
        "repo view": {
            "rc": 0,
            "stdout": json.dumps(
                {
                    "nameWithOwner": "tbhb-dev/example",
                    "url": "https://github.com/tbhb-dev/example",
                }
            ),
            "stderr": "",
        },
        "pr view": {"rc": 0, "stdout": json.dumps(data), "stderr": ""},
        BRANCH_LOOKUP: {
            "rc": 0,
            "stdout": json.dumps({"name": "agent/topic", "protected": False}),
            "stderr": "",
        },
    }


def edit_cases(
    url: object, stdout: str = "PUBLIC\n", rc: int = 0
) -> dict[str, dict[str, object]]:
    """The PR's page is ``url``; its repository reads ``stdout``.

    The working directory's repository, which a read with no repository
    argument would name, is PRIVATE, so a command that reads it gets that.
    """
    return {
        "pr view": view_case(url),
        f"repo view {TARGET}": {
            "rc": rc,
            "stdout": stdout,
            "stderr": "boom" if rc else "",
        },
        "repo view --json": {"rc": 0, "stdout": "PRIVATE\n", "stderr": ""},
    }


VISIBILITY_READ = ["--json", "visibility", "--jq", ".visibility"]


def remote_default(world: World, tmp_path: Path, branch: str) -> None:
    remote = tmp_path / "remote.git"
    world.git("init", "-q", "--bare", "-b", branch, str(remote))
    world.git("push", "-q", str(remote), f"HEAD:refs/heads/{branch}")
    world.git("remote", "set-url", "origin", str(remote))


def assert_trunk_count(counter: Mock, world: World) -> None:
    """Check that counting used the fetched default branch and pinned tools."""
    counter.assert_called_once_with(
        "refs/remotes/origin/trunk",
        "HEAD",
        default_ref=None,
        head_branch=world.git("branch", "--show-current"),
        scc=str(world.bin / "scc"),
        git=str(world.root.parent / "git_dir" / "git"),
    )


def gh_object(value: dict[str, object]) -> dict[str, object]:
    """A successful fake gh response with one JSON object."""
    return {"rc": 0, "stdout": json.dumps(value), "stderr": ""}
