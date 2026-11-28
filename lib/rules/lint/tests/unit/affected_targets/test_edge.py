# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import hashlib
import importlib
import io
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

if TYPE_CHECKING:
    from types import ModuleType


def _load(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    root = str(Path.cwd() / ".github/actions/affected-targets")
    monkeypatch.setattr(sys, "path", [root, *sys.path])
    return importlib.import_module(f"affected_targets.{name}")


@pytest.fixture
def edge(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return _load("edge", monkeypatch)


def _parse_output(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = iter(path.read_text(encoding="utf-8").splitlines())
    for line in lines:
        if "<<" in line:
            key, delim = line.split("<<", 1)
            values[key] = "\n".join(iter(lambda d=delim: next(lines, d), delim))
        else:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _assert_stopped(text: str) -> None:
    token = ""
    for line in text.splitlines():
        if not token and line.startswith("::stop-commands::"):
            token = line.removeprefix("::stop-commands::")
        elif token:
            token = "" if line == f"::{token}::" else token
        else:
            assert "::" not in line
            assert "##[" not in line
    assert not token


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    cmd = ["git", *args]
    return subprocess.run(
        cmd, cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, dict[str, str], str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    ident = {
        f"GIT_{role}_{field}": "test" if field == "NAME" else "test@example.invalid"
        for role in ("AUTHOR", "COMMITTER")
        for field in ("NAME", "EMAIL")
    }
    env = (
        os.environ
        | ident
        | {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CEILING_DIRECTORIES": str(tmp_path),
        }
    )
    _git(repo, env, "init", "-q", "-b", "main", ".")
    commits: list[str] = []
    for name in ("README.md", "docs.md"):
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
        _git(repo, env, "add", "-A")
        _git(repo, env, "-c", "commit.gpgsign=false", "commit", "-q", "-m", name)
        commits.append(_git(repo, env, "rev-parse", "HEAD"))
    return repo, env, commits[0], commits[1]


def test_env_and_deadline_exits(edge: ModuleType, tmp_path: Path) -> None:
    stripped = dict.fromkeys(edge.STRIPPED_ENV, "x")
    assert edge.stripped_env({"KEEP": "1", **stripped}) == {"KEEP": "1"}
    assert edge.deadline([sys.executable, "-c", "pass"], 0) == (124, b"")
    log = tmp_path / "tool.log"
    snippet = "import sys; print(len(sys.stdin.read())); sys.stderr.write('err\\n')"
    with log.open("ab") as handle:
        got = edge.deadline([sys.executable, "-c", snippet], 5, stderr=handle)
    assert (got, log.read_text(encoding="utf-8")) == ((0, b"0\n"), "err\n")
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    assert edge.deadline(
        [sys.executable, "-c", killed], 5, stdout=subprocess.DEVNULL
    ) == (137, b"")


def test_deadline_timeout_group_kill_and_reap(
    edge: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = tmp_path / "straggler"
    child = "import pathlib,sys,time;time.sleep(0.5);pathlib.Path(sys.argv[1]).touch()"
    spawn = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(10)"
    )
    assert edge.deadline(
        [sys.executable, "-c", spawn, child, str(mark)], 0.2, grace=1.0
    ) == (124, b"")
    assert not mark.exists()
    stubborn = "import signal as s,time;s.signal(s.SIGTERM,s.SIG_IGN);time.sleep(10)"
    assert edge.deadline([sys.executable, "-c", stubborn], 0.1, grace=0.1) == (124, b"")
    monkeypatch.setattr(edge.os, "killpg", Mock(return_value=None))
    monkeypatch.setattr(edge.time, "sleep", Mock(return_value=None))
    assert edge.deadline([sys.executable, "-c", "pass"], 5, reap_polls=1) == (0, b"")


def test_download_pins_retries_hash_and_cleanup(
    edge: ModuleType, tmp_path: Path
) -> None:
    dest = tmp_path / "bin" / "td"
    assert edge.download(dest, system="Darwin", machine="arm64") is None
    assert not dest.exists()
    amd64 = "7f3360cf38d43d4d106440e8ce9d394d4b16bde52af11cd010e897394cec439b"
    arm64 = "55d362d87b1c8d946b50ccc483d32ef48620ad1dd95eebd14e2ad41baf1baa2c"
    assert edge.TD_VERSION == "v0.34.1"
    assert edge.TD_RELEASES[("Linux", "x86_64")] == ("amd64", amd64)
    assert edge.TD_RELEASES[("Linux", "aarch64")] == ("arm64", arm64)

    payload, orig, calls = b"binary-bytes", edge.TD_RELEASES[("Linux", "x86_64")], 0

    def flaky(url: str, *, timeout: int) -> io.BytesIO:
        nonlocal calls
        assert url.endswith("/v0.34.1/target-determinator.linux.amd64")
        assert timeout > 0
        calls += 1
        if calls < 3:
            raise OSError
        return io.BytesIO(payload)

    def fetch(opener: object, retries: int = 3) -> object:
        return edge.download(
            dest, opener=opener, system="Linux", machine="x86_64", retries=retries
        )

    digest = hashlib.sha256(payload).hexdigest()
    edge.TD_RELEASES[("Linux", "x86_64")] = ("amd64", digest)
    try:
        assert (fetch(flaky), calls, dest.read_bytes()) == (dest, 3, payload)
        assert stat.S_IMODE(dest.stat().st_mode) == 0o755
        assert fetch(Mock(return_value=io.BytesIO(b"wrong"))) is None
        assert not dest.exists()
        assert fetch(Mock(side_effect=OSError), retries=2) is None
        assert not dest.exists()
    finally:
        edge.TD_RELEASES[("Linux", "x86_64")] = orig


def test_clean_and_ensure_head(
    edge: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env, base, head = _init_repo(tmp_path)
    err = io.StringIO()
    assert edge.clean(repo, env)
    assert edge.ensure_head(head, ran=False, cwd=repo, env=env, stderr=err)

    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    assert not edge.clean(repo, env)
    assert edge.ensure_head(head, ran=True, cwd=repo, env=env, stderr=err)
    assert edge.clean(repo, env)

    _git(repo, env, "config", "status.showUntrackedFiles", "no")
    (repo / "stray").touch()
    assert not edge.clean(repo, env)
    (repo / "stray").unlink()

    for flag in ("assume-unchanged", "skip-worktree"):
        _git(repo, env, "update-index", f"--{flag}", "README.md")
        assert not edge.clean(repo, env)
        _git(repo, env, "update-index", f"--no-{flag}", "README.md")

    _git(repo, env, "checkout", "-q", base)
    assert edge.ensure_head(head, ran=False, cwd=repo, env=env, stderr=err)
    assert _git(repo, env, "rev-parse", "HEAD") == head
    assert not edge.ensure_head("0" * 40, ran=False, cwd=repo, env=env, stderr=err)
    _assert_stopped(err.getvalue())

    orig_git = edge.git

    def fail_on(cmd: str) -> object:
        def wrapper(*args: str, **kw: object) -> object:
            if args[0] == cmd:
                return subprocess.CompletedProcess(args, 1, b"##[notice]err\n", b"")
            return orig_git(*args, **kw)

        return wrapper

    monkeypatch.setattr(edge, "git", fail_on("ls-files"))
    assert not edge.clean(repo, env)
    _git(repo, env, "checkout", "-q", base)
    monkeypatch.setattr(edge, "git", fail_on("submodule"))
    sub_err = io.StringIO()
    assert not edge.ensure_head(head, ran=True, cwd=repo, env=env, stderr=sub_err)
    _assert_stopped(sub_err.getvalue())


def test_finish_outputs_summary_and_restore_failure(
    edge: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decide = _load("decide", monkeypatch)
    repo, env, _base, head = _init_repo(tmp_path)
    out_file, sum_file, targets = (
        tmp_path / "output",
        tmp_path / "summary",
        tmp_path / "targets.txt",
    )
    targets.write_text("//a:t\n\n//b:t\n", encoding="utf-8")

    def run_finish(mode: str, *, ran: bool, summary: str = "") -> str:
        out = io.StringIO()
        call_env = env | {
            "GITHUB_OUTPUT": str(out_file),
            "GITHUB_STEP_SUMMARY": summary,
        }
        kw = {"cwd": repo, "env": call_env, "stdout": out, "stderr": io.StringIO()}
        edge.finish(mode, "ok", head, targets, ran=ran, **kw)
        return out.getvalue()

    _assert_stopped(run_finish("selected", ran=True))
    got = _parse_output(out_file)
    assert (got["all"], got["count"], got["reason"], got["file"]) == (
        "false",
        "2",
        "ok",
        str(targets),
    )
    assert got["targets"] == "//a:t\n//b:t"
    assert not sum_file.exists()

    (repo / "README.md").write_text("smudge\n", encoding="utf-8")
    out_file.write_text("", encoding="utf-8")
    run_finish("selected", ran=True, summary=str(sum_file))
    got = _parse_output(out_file)
    assert (got["all"], got["count"]) == ("true", "all")
    assert got["reason"] == "the checkout changed while the tools ran"
    assert got["targets"] == decide.fallback().rstrip("\n")
    assert "### Affected Bazel targets" in sum_file.read_text(encoding="utf-8")

    monkeypatch.setattr(edge, "ensure_head", Mock(return_value=False))
    with pytest.raises(edge.FatalError, match="can't restore"):
        run_finish("all", ran=False)
