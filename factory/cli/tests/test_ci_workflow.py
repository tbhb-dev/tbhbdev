"""Check that root CI tests the targets selected for the pull request."""

from pathlib import Path

import yaml


def test_ci_runs_affected_targets() -> None:
    parents = Path(__file__).resolve().parents
    workspace = next(parent.parent for parent in parents if parent.name == "factory")
    workflow = workspace / ".github/workflows/ci.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["bazel"]["steps"]
    selector = next(step for step in steps if step.get("id") == "affected")
    assert selector["uses"] == "./.github/actions/affected-targets"
    test = next(step for step in steps if step["name"] == "Test")
    assert test["env"]["TARGETS"] == "${{ steps.affected.outputs.file }}"
    assert '--target_pattern_file="$TARGETS"' in test["run"]
