"""Inspect Git history and export patches from scratch repositories."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING
lazy from pathlib import Path

import pytest
from agent_cli_example import run_agent
from agent_driver_example import (
    commit_in,
    execution_arguments,
    git_in,
    mirror_in,
    write_standalone_fake,
)
from agent_example import settings
from remote_script_example import cleanup, clone_in, execute, execute_ssh, no_session

from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote_inspection
from tbhbdev.alpha.agent.driver import arguments as agent_arguments
from tbhbdev.alpha.agent.driver import metadata
from tbhbdev.alpha.agent.driver.inspection import inspect
from tbhbdev.alpha.agent.driver.launch import prepare

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def inspect_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    limit: int | None,
    *,
    unsandboxed: bool = False,
) -> str:
    monkeypatch.setattr(transport, "execute", execute_ssh)
    args = agent_arguments.parser("example").parse_args(
        execution_arguments("example.md")
    )
    run = prepare(settings(), args)
    run.request = replace(run.request, unsandboxed=unsandboxed)
    run.owner = "example-owner"
    assert inspect(settings(), run, limit=limit) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "expected"),
    [("exit 0", 0), ("exit 7", 7), ("kill -TERM $$", 143), ("kill -INT $$", 130)],
)
def test_mirror_lock_preserves_inner_exit(
    host: Path, command: str, expected: int
) -> None:
    mirror_in(host)
    tokens = shlex.split(
        remote_inspection.mirror_command("example-org/example-repo", ":")
    )
    start = tokens.index("perl")
    result = subprocess.run(
        [*tokens[start : start + 4], str(host / "lock"), command],
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("limit", [None, 10])
def test_mirror_git_dir_never_discovers_parent(host: Path, limit: int | None) -> None:
    mirror = mirror_in(host)
    mirror.rename(host / "saved-mirror")
    mirror.mkdir()
    git_in(mirror.parent, "init")
    git_in(mirror.parent, "remote", "add", "origin", (host / "origin.git").as_uri())
    marker = host / "parent-touched"
    write_standalone_fake(
        host / "bin/git",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('git called')\n",
    )
    result = execute(
        remote_inspection.inspect("example-org/example-repo", "sample", limit=limit)
    )
    assert result.returncode == 65
    assert result.stdout == b""
    assert not marker.exists()


def test_mirror_git_host_case_shares_path(host: Path) -> None:
    mirror = mirror_in(host)
    scripts = [
        remote_inspection.inspect(
            f"{git_host}/example-org/example-repo", "sample", limit=10
        )
        for git_host in ("GitHub.com", "github.com")
    ]
    assert scripts[0] == scripts[1]
    for script in scripts:
        result = execute(script)
        assert result.returncode == 0, result.stderr
    assert list(mirror.parent.parent.parent.iterdir()) == [mirror.parent.parent]


@pytest.mark.parametrize("limit", [None, 10])
def test_inspection_fetch_does_not_write_metadata(
    host: Path, limit: int | None
) -> None:
    mirror = mirror_in(host)
    git_in(mirror, "config", "core.logAllRefUpdates", "true")
    calls = host / "git-calls.jsonl"
    write_standalone_fake(
        host / "bin/git",
        "import json, os, sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as target:\n"
        "    target.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "os.execv('/usr/bin/git', ['git', *sys.argv[1:]])\n",
    )
    fetch_head = mirror / "FETCH_HEAD"
    fetch_head.write_text("preserve fetch head\n")
    assert (
        execute(
            remote_inspection.inspect("example-org/example-repo", "sample", limit=limit)
        ).returncode
        == 0
    )
    assert git_in(mirror, "rev-parse", "refs/tbhbdev/default") == git_in(
        host / "origin.git", "rev-parse", "HEAD"
    )
    assert fetch_head.read_text() == "preserve fetch head\n"
    assert not (mirror / "logs").exists()
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    fetch = next(args for args in invocations if "fetch" in args)
    assert fetch[fetch.index("transfer.fsckObjects=true") - 1] == "-c"
    query = next(args for args in invocations if "--no-ext-diff" in args)
    # Ref spelling matters on case-sensitive hosts even when local loose refs
    # happen to accept another case.
    comparison = (
        "refs/tbhbdev/default...refs/heads/agent/sample"
        if limit is None
        else "refs/tbhbdev/default..refs/heads/agent/sample"
    )
    assert query[-2:] == [comparison, "--"]


@pytest.mark.parametrize("limit", [None, 10])
@pytest.mark.parametrize("size", [0, 65535, 65536, 65537, 200000])
def test_inspection_caps_remote_output(
    host: Path, limit: int | None, size: int
) -> None:
    mirror_in(host)
    write_standalone_fake(
        host / "bin/git",
        "import sys\n"
        "if 'ls-remote' in sys.argv: print('pushed')\n"
        f"elif 'fetch' not in sys.argv: sys.stdout.write('x' * {size})\n",
    )
    result = execute(
        remote_inspection.inspect("example-org/example-repo", "sample", limit=limit)
    )
    assert result.returncode == 0, result.stderr
    expected = b"x" * min(size, 65536)
    if size > 65536:
        expected += b"\n[output truncated at 65536 bytes]\n"
    assert result.stdout == expected


@pytest.mark.parametrize("limit", [None, 10])
def test_inspection_preserves_read_failure(host: Path, limit: int | None) -> None:
    mirror_in(host)
    write_standalone_fake(
        host / "bin/git",
        "import sys\n"
        "if 'ls-remote' in sys.argv: print('pushed')\n"
        "sys.exit(0 if 'fetch' in sys.argv or 'ls-remote' in sys.argv else 74)\n",
    )
    assert (
        execute(
            remote_inspection.inspect("example-org/example-repo", "sample", limit=limit)
        ).returncode
        == 74
    )


@pytest.mark.parametrize(
    ("component", "kind"),
    [
        (component, kind)
        for component in (
            "agent-mirrors",
            "github.com",
            "example-org",
            "example-repo.git",
        )
        for kind in ("symlink", "file", "missing")
    ]
    + [("agent-mirror.lock", "symlink")],
)
def test_mirror_refuses_symlinks(host: Path, component: str, kind: str) -> None:
    mirror = mirror_in(host)
    path = (
        mirror / component
        if component == "agent-mirror.lock"
        else next(p for p in (mirror, *mirror.parents) if p.name == component)
    )
    if path.exists():
        path.rename(host / "saved")
    if kind == "symlink":
        path.symlink_to(host / "saved")
    elif kind == "file":
        path.write_text("not a directory")
    result = execute(
        remote_inspection.inspect("example-org/example-repo", "sample", limit=10)
    )
    assert result.returncode == 65
    assert result.stdout == b""


@pytest.mark.parametrize("limit", [None, 10], ids=["diff", "log"])
def test_inspection_holds_mirror_lock_and_hardens_git(
    host: Path, limit: int | None
) -> None:
    mirror = mirror_in(host)
    write_standalone_fake(
        host / "bin/git",
        "import os, sys, fcntl\nfrom pathlib import Path\n"
        "assert os.environ['GIT_NO_LAZY_FETCH'] == '1'\n"
        "assert os.environ['GIT_CONFIG_NOSYSTEM'] == '1'\n"
        "assert os.environ['GIT_TERMINAL_PROMPT'] == '0'\n"
        "assert os.environ['GIT_ASKPASS'] == '/bin/false'\n"
        "assert os.environ['SSH_ASKPASS'] == '/bin/false'\n"
        "assert os.environ['LANG'] == os.environ['LC_ALL'] == 'C'\n"
        "options = {}\nargs = sys.argv[1:]\n"
        "while args and args[0] == '-c':\n"
        "    key, value = args[1].split('=', 1)\n"
        "    options[key] = value\n    args = args[2:]\n"
        "assert options == {\n"
        "    'core.hooksPath': '/dev/null', 'core.fsmonitor': 'false',\n"
        "    'core.sshCommand': 'ssh', 'credential.helper': '', 'core.askPass': '',\n"
        "    'protocol.ext.allow': 'never', 'core.pager': 'cat',\n"
        "    'gc.auto': '0', 'maintenance.auto': 'false',\n"
        "    'fetch.recurseSubmodules': 'false', 'submodule.recurse': 'false'}\n"
        f"assert args[0] == {('--git-dir=' + str(mirror))!r}\n"
        f"with Path({str(mirror / 'agent-mirror.lock')!r}).open('r') as lock:\n"
        "    try:\n        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n        pass\n"
        "    else:\n        raise AssertionError('mirror lock required')\n"
        "os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n",
    )
    result = execute(
        remote_inspection.inspect("example-org/example-repo", "sample", limit=limit)
    )
    assert result.returncode == 0, result.stderr
    assert b"pushed change" in result.stdout


@pytest.mark.parametrize("limit", [None, 1, 10])
def test_inspection_reads_pushed_history(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    limit: int | None,
) -> None:
    mirror = mirror_in(host)
    source = host / "source"
    git_in(source, "checkout", "agent/sample")
    (source / "example").write_text("second pushed change\n")
    hostile_path = "path-\x1b[31m"
    (source / hostile_path).write_text("hostile file \x1b[33m\n")
    git_in(source, "add", "example", hostile_path)
    commit_in(source, "second change \x1b[31m", "Example \x1b[32m")
    git_in(source, "push", (host / "origin.git").as_uri(), "agent/sample")
    git_in(source, "checkout", "main")
    (source / "default-only").write_text("new default tip\n")
    git_in(source, "add", "default-only")
    commit_in(source, "default branch advanced")
    git_in(source, "push", (host / "origin.git").as_uri(), "main")
    # Even host settings that normally prune cannot drop mirror history.
    git_in(mirror, "branch", "preserved")
    git_in(mirror, "config", "fetch.prune", "true")
    git_in(mirror, "config", "core.quotePath", "false")
    # Git quotes control characters in paths, but stores the literal name.
    assert hostile_path in git_in(
        source, "ls-tree", "-z", "--name-only", "agent/sample"
    )
    raw = git_in(source, "show", "--format=raw", "agent/sample")
    assert "second change \x1b[31m" in raw
    assert "Example \x1b[32m" in raw
    output = inspect_output(monkeypatch, capsys, limit)
    assert "second" in output
    assert "default branch advanced" not in output
    assert "default-only" not in output
    assert "\x1b" not in output
    if limit is None:
        assert "-base\n+second pushed change\n" in output
        assert "+hostile file [33m\n" in output
    else:
        assert output.count("commit ") == (1 if limit == 1 else 2)
    assert git_in(mirror, "rev-parse", "refs/tbhbdev/default") == git_in(
        source, "rev-parse", "main"
    )
    assert git_in(mirror, "rev-parse", "preserved")


@pytest.mark.parametrize("verb", ["clean", "log", "diff"])
def test_run_clone_config_never_executes(host: Path, verb: str) -> None:
    mirror_in(host)
    clone = clone_in(host)
    tree = clone / ".worktrees/agent-sample"
    marker = host / "executed"
    command = host / "plant"
    command.write_text(f"#!/bin/sh\nprintf planted > {shlex.quote(str(marker))}\n")
    command.chmod(0o700)
    config = (
        f"[core]\nfsmonitor = {command}\npager = {command}\n"
        f"[alias]\nlog = !{command}\ndiff = !{command}\n"
        f'[filter "example"]\nclean = {command}\nsmudge = {command}\n'
        f"[diff]\nexternal = {command}\n"
    )
    (tree / ".git/config").write_text(config)
    hooks = tree / ".git/hooks"
    hooks.mkdir()
    (hooks / "post-index-change").symlink_to(command)
    (hooks / "reference-transaction").symlink_to(command)
    (clone / ".git").mkdir()
    (clone / ".git/config").write_text(config)
    # Any git at either agent-written location fails this test, even if hardened.
    write_standalone_fake(
        host / "bin/git",
        "import os, sys\n"
        f"assert {str(clone)!r} not in os.getcwd()\n"
        f"assert not any({str(clone)!r} in arg for arg in sys.argv)\n"
        "os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n",
    )
    no_session(host)
    script = (
        cleanup()
        if verb == "clean"
        else remote_inspection.inspect(
            "example-org/example-repo",
            "sample",
            limit=10 if verb == "log" else None,
        )
    )
    assert execute(script).returncode == 0
    assert not marker.exists()
    assert (clone / ".git/config").read_text() == config
    if verb == "clean":
        assert (
            host / "state/tbhbdev/agent-runs/trash/stamp-sample/worktree/.git/config"
        ).read_text() == config


@pytest.mark.parametrize(
    ("limit", "unpublished"), [(None, False), (10, False), (None, True)]
)
def test_inspection_disables_external_diff_and_text_conversion(
    host: Path, limit: int | None, unpublished: bool
) -> None:
    if unpublished:
        tree = unpublished_run(host)
        (tree / "example").write_text("local change\n")
        git_in(tree, "add", "example")
        commit_in(tree, "local change")
        mirror = tree / ".git"
    else:
        mirror = mirror_in(host)
    marker = host / "executed"
    command = host / "plant"
    command.write_text(f"#!/bin/sh\nprintf planted > {shlex.quote(str(marker))}\n")
    command.chmod(0o700)
    git_in(mirror, "config", "diff.external", str(command))
    git_in(mirror, "config", "diff.example.textconv", str(command))
    (mirror / "info/attributes").write_text("* diff=example\n")
    script = remote_inspection.inspect(
        "example-org/example-repo", "sample", limit=limit
    )
    tokens = shlex.split(script)
    inner = tokens[-1]
    assert " -c diff.external= --no-optional-locks " in inner
    assert "--no-ext-diff --no-textconv --no-color" in inner
    assert "--no-prune --no-prune-tags" in inner
    assert execute(script).returncode == 0
    assert not marker.exists()


@pytest.mark.parametrize("limit", [None, 10])
def test_inspection_refuses_missing_origin_branch(
    host: Path, limit: int | None
) -> None:
    mirror_in(host)
    git_in(host / "origin.git", "update-ref", "-d", "refs/heads/agent/sample")
    result = execute(
        remote_inspection.inspect("example-org/example-repo", "sample", limit=limit)
    )
    assert result.returncode != 0
    assert result.stdout == b""


def unpublished_run(host: Path) -> Path:
    mirror = mirror_in(host)
    # A mirror initialized before fetching need not have a valid default HEAD.
    git_in(mirror, "symbolic-ref", "HEAD", "refs/heads/unborn")
    tree = host / "Code/github.com/example-org/example-repo/.worktrees/agent-sample"
    git_in(host, "clone", str(mirror), str(tree))
    git_in(tree, "checkout", "-b", "agent/sample", "origin/main")
    git_in(host / "origin.git", "update-ref", "-d", "refs/heads/agent/sample")
    return tree


def fallback_diff() -> subprocess.CompletedProcess[bytes]:
    return execute(
        remote_inspection.inspect("example-org/example-repo", "sample", limit=None)
    )


def assert_refused() -> None:
    result = fallback_diff()
    assert result.returncode == 65
    assert result.stdout == b""


@pytest.mark.parametrize("unsandboxed", [False, True])
@pytest.mark.parametrize("commits", [0, 2])
def test_diff_returns_unpublished_commits(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unsandboxed: bool,
    commits: int,
) -> None:
    tree = unpublished_run(host)
    base = git_in(tree, "rev-parse", "origin/main")
    for index in range(commits):
        (tree / "example").write_text(f"local change {index}\n")
        git_in(tree, "add", "example")
        commit_in(tree, f"local commit {index} \x1b[31m")
    tip = git_in(tree, "rev-parse", "HEAD")
    (tree / "example").write_text("uncommitted change\n")
    (tree / "untracked").write_text("untracked change\n")
    source = host / "source"
    (source / "upstream-only").write_text("new default tip\n")
    git_in(source, "add", "upstream-only")
    commit_in(source, "upstream only")
    git_in(source, "push", str(host / "origin.git"), "main")
    # The mirror still has the old pushed agent branch; it must not be used.
    before = git_in(tree, "status", "--porcelain")
    output = inspect_output(monkeypatch, capsys, None, unsandboxed=unsandboxed)
    assert "\x1b" not in output
    assert "upstream-only" not in output
    assert "pushed change" not in output
    assert "uncommitted" not in output
    assert "untracked" not in output
    assert git_in(tree, "status", "--porcelain") == before
    assert git_in(tree, "rev-parse", "origin/main") == base
    if commits == 0:
        assert output == ""
    else:
        assert output.count("Subject: [PATCH") == commits
        assert f"From {tip} " in output
        patch = host / "commits.patch"
        patch.write_text(output)
        target = host / "apply"
        git_in(host, "clone", str(host / "origin.git"), str(target))
        git_in(target, "reset", "--hard", base)
        git_in(
            target,
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "am",
            str(patch),
        )
        assert (target / "example").read_text() == "local change 1\n"


@pytest.mark.parametrize("failure", [False, True])
def test_unpublished_diff_bounds_output_and_preserves_failure(
    host: Path, failure: bool
) -> None:
    unpublished_run(host)
    write_standalone_fake(
        host / "bin/git",
        "import sys\nfrom pathlib import Path\n"
        "if '--symref' in sys.argv:\n"
        "    print('ref: refs/heads/main\\tHEAD'); sys.exit(0)\n"
        "if 'ls-remote' in sys.argv: sys.exit(0)\n"
        "if 'init' in sys.argv:\n"
        "    (Path(sys.argv[-1]) / 'objects/info').mkdir(parents=True); sys.exit(0)\n"
        "if 'rev-list' in sys.argv or 'merge-base' in sys.argv: sys.exit(0)\n"
        "assert 'format-patch' in sys.argv\n"
        "assert 'diff.external=' in sys.argv\n"
        "assert '--no-ext-diff' in sys.argv and '--no-textconv' in sys.argv\n"
        "assert '--stdout' in sys.argv and '--no-optional-locks' in sys.argv\n"
        "assert '--no-signature' in sys.argv\n"
        "assert 'protocol.allow=never' in sys.argv\n"
        "sys.stdout.write('x' * 200000)\n"
        f"sys.exit({74 if failure else 0})\n",
    )
    result = fallback_diff()
    assert result.returncode == (74 if failure else 0)
    assert result.stdout == b"x" * 65536 + b"\n[output truncated at 65536 bytes]\n"


@pytest.mark.parametrize("entry", [".git", ".git/objects/pack"])
def test_unpublished_diff_withholds_patch_if_a_symlink_appears(
    host: Path, entry: str
) -> None:
    path = unpublished_run(host) / entry
    write_standalone_fake(
        host / "bin/git",
        "import os, sys\n"
        "from pathlib import Path\n"
        "if 'format-patch' not in sys.argv:\n"
        "    os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n"
        f"path = Path({str(path)!r})\n"
        f"path.rename({str(host / 'saved')!r})\n"
        f"path.symlink_to({str(host / 'missing')!r}, target_is_directory=True)\n"
        "print('generated patch')\n",
    )
    assert_refused()
    assert path.is_symlink()


@pytest.mark.parametrize(
    "component",
    [
        "Code",
        "github.com",
        "example-org",
        "example-repo",
        ".worktrees",
        "agent-sample",
        ".git",
    ],
)
def test_unpublished_diff_refuses_symlink_paths(host: Path, component: str) -> None:
    tree = unpublished_run(host)
    path = next(
        path for path in (tree / ".git", tree, *tree.parents) if path.name == component
    )
    path.rename(host / "saved")
    path.symlink_to(host / "saved", target_is_directory=True)
    assert_refused()


@pytest.mark.parametrize(
    ("key", "content"),
    [
        ("format.signatureFile", "SECRET-LINE\n"),
        ("include.path", "[user]\n\temail = SECRET-LINE@example.invalid\n"),
    ],
)
def test_unpublished_diff_ignores_the_run_config(
    host: Path, key: str, content: str
) -> None:
    tree = unpublished_run(host)
    (host / "private.cfg").write_text(content)
    git_in(tree, "config", key, str(host / "private.cfg"))
    git_in(tree, "config", "format.from", "true")
    git_in(tree, "config", "format.headers", "X-Leak: header-value")
    (tree / "example").write_text("local change\n")
    git_in(tree, "add", "example")
    commit_in(tree, "local change")
    result = fallback_diff()
    assert result.returncode == 0, result.stderr
    assert b"Subject: [PATCH" in result.stdout
    assert b"SECRET-LINE" not in result.stdout
    assert b"X-Leak" not in result.stdout


@pytest.mark.parametrize("nested", ["objects/pack", "objects/ab", "refs/heads/dir"])
def test_unpublished_diff_refuses_nested_symlinks(host: Path, nested: str) -> None:
    tree = unpublished_run(host)
    target = tree / ".git" / nested
    target.mkdir(parents=True, exist_ok=True)
    (target / "link").symlink_to(host / "saved")
    assert_refused()


@pytest.mark.parametrize("hidden", ["objects", "refs", "objects/pack"])
def test_unpublished_diff_refuses_unreadable_directories(
    host: Path, hidden: str
) -> None:
    tree = unpublished_run(host)
    target = tree / ".git" / hidden
    target.chmod(0o100)
    try:
        assert_refused()
    finally:
        target.chmod(0o700)


@pytest.mark.parametrize(
    "name",
    [
        "objects/info/alternates",
        "objects/pack/multi-pack-index",
        "objects/pack/multi-pack-index-abc.bitmap",
    ],
)
def test_unpublished_diff_refuses_object_store_redirects(host: Path, name: str) -> None:
    tree = unpublished_run(host)
    (tree / ".git" / name).parent.mkdir(parents=True, exist_ok=True)
    (tree / ".git" / name).write_text(str(host / "origin.git") + "\n")
    assert_refused()


def test_unpublished_diff_ignores_a_commondir_file(host: Path) -> None:
    tree = unpublished_run(host)
    other = host / "private-repo"
    git_in(host, "init", "-q", str(other))
    (tree / "example").write_text("local change\n")
    git_in(tree, "add", "example")
    commit_in(tree, "local change")
    (tree / ".git/commondir").write_text(str(other) + "\n")
    result = fallback_diff()
    assert result.returncode == 0, result.stderr
    assert b"Subject: [PATCH" in result.stdout


@pytest.mark.parametrize("entry", remote_inspection.GIT_INTERNALS)
def test_unpublished_diff_refuses_symlinked_git_internals(
    host: Path, entry: str
) -> None:
    tree = unpublished_run(host)
    path = tree / ".git" / entry
    if not path.exists():
        path.write_text("")
    path.rename(host / "saved")
    path.symlink_to(host / "saved")
    assert_refused()


def test_unpublished_diff_refuses_missing_base(host: Path) -> None:
    tree = unpublished_run(host)
    (tree / ".git/logs/refs/heads/agent/sample").unlink()
    result = fallback_diff()
    assert result.returncode != 0
    assert result.stdout == b""


def test_unpublished_diff_ignores_default_branch_rename(host: Path) -> None:
    tree = unpublished_run(host)
    git_in(tree, "update-ref", "-d", "refs/remotes/origin/main")
    origin = host / "origin.git"
    git_in(origin, "update-ref", "refs/heads/trunk", "main")
    git_in(origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
    (tree / "example").write_text("local trunk change\n")
    git_in(tree, "add", "example")
    commit_in(tree, "local trunk change")
    result = fallback_diff()
    assert result.returncode == 0, result.stderr
    assert b"+local trunk change" in result.stdout


@pytest.mark.parametrize("rc", [0, 124])
def test_diff_falls_back_when_origin_is_unavailable(host: Path, rc: int) -> None:
    tree = unpublished_run(host)
    (tree / "example").write_text("offline change\n")
    git_in(tree, "add", "example")
    commit_in(tree, "offline change")
    mirror = (
        host / "state/tbhbdev/agent-mirrors/github.com/example-org/example-repo.git"
    )
    git_in(mirror, "config", "remote.origin.url", str(host / "missing.git"))
    (host / "state/tbhbdev/agent-runs/sample.rc").write_text(f"{rc}\n")
    result = fallback_diff()
    assert result.returncode == 0, result.stderr
    assert b"+offline change\n" in result.stdout
    assert bool(result.stderr) is (rc == 0)


def merge_run_changes(tree: Path, resolution: bytes) -> None:
    example = tree / "example"
    git_in(tree, "switch", "-c", "topic")
    example.write_text("side change\n")
    git_in(tree, "add", "example")
    commit_in(tree, "side change")
    git_in(tree, "switch", "agent/sample")
    example.write_text("run change\n")
    git_in(tree, "add", "example")
    commit_in(tree, "unpublished change")
    with pytest.raises(subprocess.CalledProcessError):
        git_in(tree, "merge", "--no-edit", "topic")
    assert git_in(tree, "status", "--porcelain") == "UU example"
    example.write_bytes(resolution)
    git_in(tree, "add", "example")
    commit_in(tree, "resolve merge")


def reset_then_add(tree: Path, content: bytes) -> str:
    (tree / "base-only").write_text("present on the recorded base\n")
    git_in(tree, "add", "base-only")
    commit_in(tree, "add base-only file")
    base = git_in(tree, "rev-parse", "HEAD")
    git_in(tree, "reset", "--hard", "HEAD^")
    (tree / "added").write_bytes(content)
    git_in(tree, "add", "added")
    commit_in(tree, "add unrelated file")
    return base


def mock_agent_run_for_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "execute", execute_ssh)
    config = settings()
    request_args = agent_arguments.parser("example").parse_args(
        execution_arguments("example.md")
    )
    run = prepare(config, request_args)
    run.owner = "example-owner"

    def load(_settings: Settings, _name: str) -> Run:
        return run

    monkeypatch.setattr(agent_settings, "load", lambda: config)
    monkeypatch.setattr(metadata, "load", load)


def test_patch_export_refuses_unconfirmed_shutdown(host: Path) -> None:
    tree = unpublished_run(host)
    (tree / "example").write_text("unpublished change\n")
    git_in(tree, "add", "example")
    commit_in(tree, "unpublished change")
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.rc").write_text("124\n")
    (state / "sample.phase").write_text("unknown")

    result = execute(
        remote_inspection.export_patch("example-org/example-repo", "sample")
    )

    assert result.returncode == 65
    assert result.stdout == b""


@pytest.mark.parametrize(
    "history", ["linear", "merge", "empty merge", "reset then add"]
)
def test_finished_build_exports_complete_patch_offline(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
    history: str,
) -> None:
    tree = unpublished_run(host)
    base = git_in(tree, "rev-parse", "HEAD")
    git_in(tree, "config", "user.name", "Example")
    git_in(tree, "config", "user.email", "example@example.com")
    example = tree / "example"
    content = b"unpublished \x80\x1b\n" * 5000
    if history == "linear":
        example.write_bytes(content)
        git_in(tree, "add", "example")
        commit_in(tree, "unpublished change")
    elif history == "reset then add":
        base = reset_then_add(tree, content)
    else:
        merge_run_changes(
            tree, example.read_bytes() if history == "empty merge" else content
        )
    tip = git_in(tree, "rev-parse", "HEAD")
    tip_tree = git_in(tree, "rev-parse", "HEAD^{tree}")
    git_in(tree, "checkout", "--detach", base)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.base").write_text(base)
    (state / "sample.head").write_text(tip)
    (state.parent / "agent-mirrors").rename(host / "offline")

    mock_agent_run_for_diff(monkeypatch)
    args = ["diff", "sample", "--patch"]
    (state / "sample.phase").write_text("ended")
    assert run_agent(args) == 1
    assert not capsysbinary.readouterr().out

    (state / "sample.rc").write_text("0\n")
    assert run_agent(args) == 0
    output = capsysbinary.readouterr().out
    if history == "empty merge":
        assert output == b""
    else:
        if history == "linear":
            assert output.startswith(f"From {tip} ".encode())
        assert len(output) > 65536
        patch = host / "run.patch"
        patch.write_bytes(output)
        git_in(tree, "am", str(patch))
    if history == "reset then add":
        assert (tree / "added").read_bytes() == content
        assert not (tree / "base-only").exists()
    assert git_in(tree, "rev-parse", "HEAD^{tree}") == tip_tree
