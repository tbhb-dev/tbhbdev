"""Check that root CI tests the targets selected for the pull request."""

import os
import subprocess
from pathlib import Path

import yaml


def test_ci_shards_affected_targets(tmp_path: Path) -> None:
    parents = Path(__file__).resolve().parents
    workspace = next(parent.parent for parent in parents if parent.name == "factory")
    workflow = workspace / ".github/workflows/ci.yml"
    jobs = yaml.safe_load(workflow.read_text())["jobs"]
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
