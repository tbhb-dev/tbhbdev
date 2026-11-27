"""Exercise the shell steps in the root CI workflow."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


def _mapping(value: object) -> dict[object, object]:
    assert isinstance(value, dict)
    return value


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _workflow_jobs() -> dict[object, object]:
    parents = Path(__file__).resolve().parents
    workspace = next(parent.parent for parent in parents if parent.name == "factory")
    workflow_path = workspace / ".github/workflows/ci.yml"
    workflow = _mapping(yaml.safe_load(workflow_path.read_text()))
    return _mapping(workflow["jobs"])


def _workflow_steps(job: object) -> list[dict[object, object]]:
    steps = _list(_mapping(job)["steps"])
    return [_mapping(step) for step in steps]


def _shell_command(step: dict[object, object], prefix: str = "") -> list[str]:
    shell = step["shell"]
    script = step["run"]
    assert isinstance(shell, str)
    assert isinstance(script, str)
    return [shell, "-eo", "pipefail", "-c", prefix + script]


def test_ci_shards_affected_targets(tmp_path: Path) -> None:
    jobs = _workflow_jobs()
    bazel = _mapping(jobs["bazel"])
    strategy = _mapping(bazel["strategy"])
    shards = _list(_mapping(strategy["matrix"])["shard"])
    assert shards == [1, 2, 3, 4]
    assert strategy["fail-fast"] is False
    assert "bazel" in _list(_mapping(jobs["conclusion"])["needs"])

    steps = _workflow_steps(bazel)
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
            assert isinstance(shard, int)
            run = subprocess.run(
                _shell_command(test, fake_bazel),
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
    steps = _workflow_steps(_workflow_jobs()["prose"])
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
        _shell_command(lint),
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
