"""Exercise the shell steps in the root CI workflow."""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


def _workflow_jobs() -> dict[str, Any]:
    parents = Path(__file__).resolve().parents
    workspace = next(parent.parent for parent in parents if parent.name == "factory")
    workflow = workspace / ".github/workflows/ci.yml"
    return yaml.safe_load(workflow.read_text())["jobs"]


def test_ci_shards_affected_targets(tmp_path: Path) -> None:
    jobs = _workflow_jobs()
    strategy = jobs["bazel"]["strategy"]
    shards = strategy["matrix"]["shard"]
    assert shards == [1, 2, 3, 4]
    assert strategy["fail-fast"] is False
    assert "bazel" in jobs["conclusion"]["needs"]

    steps = jobs["bazel"]["steps"]
    selector = next(step for step in steps if step.get("id") == "affected")
    assert selector["uses"] == "./.github/actions/affected-targets"
    test = next(step for step in steps if step["name"] == "Test")
    assert test["env"] == {
        "TARGETS": "${{ steps.affected.outputs.file }}",
        "SHARD": "${{ matrix.shard }}",
        "SHARD_COUNT": "${{ strategy.job-total }}",
    }

    targets = tmp_path / "affected targets.txt"
    fake_bazel = """bazel() {
        [[ "$*" == "test --config=ci --build_tests_only --target_pattern_file="* ]]
        cat "${4#--target_pattern_file=}"
    }
    """
    no_exclusions: list[str] = []
    empty_case: tuple[list[str], list[str]] = ([], [])
    for selected, exclusions in (
        (["//:d", "//:a", "//:e", "//:b", "//:c"], no_exclusions),
        (["@rules//...", "//..."], ["-//:manual", "-@rules//:manual"]),
        empty_case,
    ):
        targets.write_text("\n".join([*selected, *exclusions]))
        for shard in shards:
            run = subprocess.run(
                [test["shell"], "-eo", "pipefail", "-c", fake_bazel + test["run"]],
                env={
                    **os.environ,
                    "TARGETS": str(targets),
                    "RUNNER_TEMP": str(tmp_path),
                    "SHARD": str(shard),
                    "SHARD_COUNT": str(len(shards)),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            expected = sorted(selected)[shard - 1 :: len(shards)]
            expected_output = (
                [*expected, *exclusions]
                if expected
                else [f"No targets in shard {shard}."]
            )
            assert run.stdout.splitlines() == expected_output


@pytest.mark.parametrize("case", ["listed", "unlisted", "missing"])
def test_ci_prose_skip_list(tmp_path: Path, case: str) -> None:
    steps = _workflow_jobs()["prose"]["steps"]
    lint = next(step for step in steps if step["name"] == "Lint prose")

    imported_copy = "imported [guide].md"
    (tmp_path / imported_copy).write_text("A prose finding.\n")
    (tmp_path / "clean.md").write_text("Clean prose.\n")
    skip_list = tmp_path / ".github/prose-lint-debt.txt"
    skip_list.parent.mkdir()
    entries = ["# Example import", ""]
    if case != "unlisted":
        entries.append(imported_copy)
    if case == "missing":
        entries.append("removed.md")
    skip_list.write_text("\n".join(entries) + "\n")

    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True)

    fake_linter = r"""#!/bin/bash
for path in "$@"; do
    [[ $path == *.md ]] || continue
    printf '%s: %s\n' "${0##*/}" "$path"
    if grep -qF 'A prose finding.' "$path"; then
        exit 1
    fi
done
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ["vale", "cspell", "rumdl"]:
        tool = bin_dir / name
        tool.write_text(fake_linter)
        tool.chmod(0o755)

    run = subprocess.run(
        [lint["shell"], "-eo", "pipefail", "-c", lint["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    if case == "listed":
        assert run.returncode == 0, run.stderr
        assert run.stdout.splitlines() == [
            "vale: clean.md",
            "cspell: clean.md",
            "rumdl: clean.md",
        ]
    elif case == "unlisted":
        assert run.returncode != 0
        assert f"vale: {imported_copy}" in run.stdout.splitlines()
    else:
        assert run.returncode != 0
        assert (
            "Prose skip list entry must name an existing Markdown file: removed.md"
            in run.stderr
        )
