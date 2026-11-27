"""Check how root CI verifies commits and tests the selected targets."""

import os
import subprocess
from pathlib import Path

import yaml


def workflow_source() -> str:
    parents = Path(__file__).resolve().parents
    workspace = next(parent.parent for parent in parents if parent.name == "factory")
    return (workspace / ".github/workflows/ci.yml").read_text()


def test_ci_runs_affected_targets() -> None:
    steps = yaml.safe_load(workflow_source())["jobs"]["bazel"]["steps"]
    selector = next(step for step in steps if step.get("id") == "affected")
    assert selector["uses"] == "./.github/actions/affected-targets"
    test = next(step for step in steps if step["name"] == "Test")
    assert test["env"]["TARGETS"] == "${{ steps.affected.outputs.file }}"
    assert '--target_pattern_file="$TARGETS"' in test["run"]


def test_ci_trailer_verifier_checkout(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    verifier = Path("factory/cli/src/tbhbdev/alpha/verify_range.py")
    source = candidate / verifier
    source.parent.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GITHUB_WORKSPACE": str(candidate),
        "RUNNER_TEMP": str(tmp_path),
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
    }

    def git(*args: str) -> str:
        config = ["-c", "user.name=Example", "-c", "user.email=test@example.com"]
        return subprocess.check_output(
            ["git", *config, *args], cwd=candidate, env=env, text=True
        ).strip()

    git("init", "--quiet")
    revisions = {}
    for label in ("legacy", "base", "candidate"):
        source.write_text(label if label == "legacy" else f'"--identity-only"\n{label}')
        git("add", str(verifier))
        git("commit", "--quiet", "-m", label)
        revisions[label] = git("rev-parse", "HEAD")
    uv = tmp_path / "uv"
    uv.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@"\ntail -n 1 {verifier}\n')
    uv.chmod(0o755)
    steps = yaml.safe_load(workflow_source())["jobs"]["bazel"]["steps"]
    step = next(s for s in steps if s["name"] == "Verify commit identity trailers")
    args = ["alpha", "verify-range", "--identity-only", "-C", str(candidate)]
    head = revisions["candidate"]
    for base, expected in (("legacy", "candidate"), ("base", "base")):
        out = subprocess.check_output(
            ["bash", "-e", "-c", step["run"]],
            cwd=candidate,
            env={**env, "BASE_SHA": revisions[base], "HEAD_SHA": head},
            text=True,
        )
        assert ("Falling back to the candidate copy." in out) == (base == "legacy")
        assert out.splitlines()[-8:] == [*args, revisions[base], head, expected]
