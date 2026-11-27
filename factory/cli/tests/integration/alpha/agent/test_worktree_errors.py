"""Safe error reporting for worktree preparation failures."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from agent_host_example import (
    constant,
    run_request,
)

from tbhbdev.alpha.agent import (
    host_tools,
    process_group,
    runfiles,
    worktree_checkout,
    worktree_clone,
    worktree_errors,
    worktree_git,
    worktree_mirror,
)
from tbhbdev.alpha.agent.model import Invalid


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (
            Invalid("host executable prek was not found on PATH /usr/bin:/bin"),
            "host executable prek was not found on PATH bin",
        ),
        (
            OSError(13, "Permission denied", "/home/example/private/hooks"),
            "Permission denied: hooks",
        ),
        (
            OSError("cannot read /home/example/private/commondir"),
            "cannot read commondir",
        ),
        (OSError("cannot read /root"), "cannot read root"),
        (OSError(5, "Input/output error"), "Input/output error"),
        (
            OSError(5, "Input/output error", b"/home/example/private/HEAD"),
            "Input/output error: HEAD",
        ),
        (ValueError("sensitive-input"), "invalid value"),
        (subprocess.SubprocessError("sensitive-command"), "git command failed"),
        (subprocess.CalledProcessError(1, "sensitive-command"), "git command failed"),
        (
            subprocess.CalledProcessError(1, "sensitive-command", stderr=""),
            "git command failed",
        ),
        (
            subprocess.CalledProcessError(
                1, "sensitive-command", stderr="fatal: no access\nprivate detail"
            ),
            "fatal: no access",
        ),
        (
            subprocess.TimeoutExpired(
                "sensitive-command", 5, stderr=b"fatal: bad \xff\nprivate detail"
            ),
            "fatal: bad \ufffd",
        ),
        (Invalid("bad\x00\t\x1b\x7f\u200b value"), "bad value"),
        (Invalid(""), "cause unavailable"),
        (Invalid("\x00\x7f"), "cause unavailable"),
        (Invalid("a" * 199), "a" * 199),
        (Invalid("a" * 200), "a" * 200),
        (Invalid("a" * 201), "a" * 200),
    ],
)
def test_preparation_cause(error: Exception, cause: str) -> None:
    with pytest.raises(Invalid) as caught, worktree_errors.preparation_step("example"):
        raise error
    assert str(caught.value) == f"run preparation failed at example: {cause}"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_preparation_success_and_unexpected_error() -> None:
    with worktree_errors.preparation_step("example"):
        pass
    message = "unexpected"
    with (
        pytest.raises(RuntimeError, match="unexpected"),
        worktree_errors.preparation_step("example"),
    ):
        raise RuntimeError(message)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            "fatal: https://x-access-token:SECRET@github.com/org/repo",
            "fatal: https://[redacted]@github.com/org/repo",
        ),
        (
            "fatal: ssh://SECRET@github.com/org/repo",
            "fatal: ssh://[redacted]@github.com/org/repo",
        ),
        (
            "fatal: https://name:pass@word@github.com/org/repo",
            "fatal: https://[redacted]@github.com/org/repo",
        ),
        ("fatal: https://github.com/org/repo", "fatal: https://github.com/org/repo"),
        ("Authorization: Basic example", "[redacted]"),
        ("Authorization=Bearer example-secret", "[redacted]"),
        (
            "token=example-secret password:example-secret secret=example-secret",
            "[redacted] [redacted] [redacted]",
        ),
        ("ghp_example github_pat_example", "[redacted] [redacted]"),
        ("credential=environment-value", "credential=[redacted]"),
        ("environment-value-long environment-value", "[redacted] [redacted]"),
        ("a-environment-value environment-value", "[redacted] [redacted]"),
        (
            "fatal: " + "a" * 190 + "https://x-access-token:SECRET@github.com/org/repo",
            "fatal: " + "a" * 190 + "htt",
        ),
    ],
)
def test_preparation_redacts_before_truncation(
    monkeypatch: pytest.MonkeyPatch, detail: str, expected: str
) -> None:
    # A controlled environment also proves that empty values leave text intact.
    monkeypatch.setattr(
        os,
        "environ",
        {
            "EXAMPLE_CREDENTIAL": "environment-value",
            "EXAMPLE_LONG_CREDENTIAL": "environment-value-long",
            "EXAMPLE_PREFIXED_CREDENTIAL": "a-environment-value",
            "EXAMPLE_EMPTY": "",
        },
    )
    with pytest.raises(Invalid) as caught, worktree_errors.preparation_step("example"):
        raise subprocess.CalledProcessError(1, "sensitive-command", stderr=detail)
    assert str(caught.value) == f"run preparation failed at example: {expected}"


def test_missing_prek_reaches_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PATH", host_tools.search())
    monkeypatch.setattr(worktree_git, "git", constant(""))
    monkeypatch.setattr(worktree_mirror, "configure_mirror", constant(None))
    monkeypatch.setattr(worktree_mirror, "fetch_commit", constant("a" * 40))
    monkeypatch.setattr(worktree_clone, "clone_run", constant(None))
    monkeypatch.setattr(host_tools.shutil, "which", constant(None))
    monkeypatch.setattr(process_group, "boot_id", constant("example-boot"))
    (tmp_path / ".worktrees" / "agent-example" / ".git").mkdir(parents=True)
    files = runfiles.RunFiles(tmp_path / "runs", "example")
    with pytest.raises(Invalid) as caught:
        process_group.prepare(
            lambda: worktree_checkout.create(tmp_path, "example", run_request()),
            files,
            time.monotonic() + 5,
            0,
        )
    assert str(caught.value) == (
        "run preparation failed at hook install: host executable prek was not found "
        "on PATH [redacted]"
    )


def test_git_stderr_reaches_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_tools, "resolve", constant("/fixed/git"))

    def output(command: list[str], **kwargs: object) -> str:
        assert kwargs["stderr"] == subprocess.PIPE
        raise subprocess.CalledProcessError(
            128,
            command,
            stderr=(
                "fatal: https://x-access-token:SECRET@github.com/org/repo\n"
                "private detail"
            ),
        )

    monkeypatch.setattr(subprocess, "check_output", output)
    with (
        pytest.raises(Invalid) as caught,
        worktree_errors.preparation_step("mirror init"),
    ):
        worktree_git.git(tmp_path, "init", "--bare")
    assert str(caught.value) == (
        "run preparation failed at mirror init: "
        "fatal: https://[redacted]@github.com/org/repo"
    )
