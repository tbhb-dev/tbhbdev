# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

from pathlib import Path

import pytest

from lint.check_lint_baselines import REGISTRY, check

ROOT = Path(__file__).absolute().parents[2]


def test_lint_baseline_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    registry: dict[str, object] = {
        "counts.toml": "counts",
        "pyproject.toml": "python-project",
    }
    base: dict[str, object] = {"module_lines": {"a.py": 320}, "noqa": {"S105": 2}}

    def project(edges: list[str], limit: int) -> dict[str, object]:
        return {
            "tool": {
                "importlinter": {
                    "contracts": [{"id": "example", "ignore_imports": edges}]
                },
                "pylint": {"format": {"max-module-lines": limit}},
            }
        }

    docs: dict[tuple[str, str], dict[str, object]] = {
        ("base", REGISTRY): registry,
        ("head", REGISTRY): registry,
        ("base", "counts.toml"): base,
        ("base", "pyproject.toml"): project(["a -> b"], 300),
        ("head", "counts.toml"): {"module_lines": {"a.py": 321}, "noqa": {"S105": 3}},
        ("head", "pyproject.toml"): project(["a -> c"], 301),
    }

    def read_at(_root: Path, revision: str, path: str) -> dict[str, object]:
        return docs[revision, path]

    monkeypatch.setattr("lint.check_lint_baselines.read_at", read_at)
    assert len(check(ROOT, "base", "head")) == 4

    docs["head", "counts.toml"] = {"module_lines": {"a.py": 319}, "noqa": {"S105": 1}}
    docs["head", "pyproject.toml"] = project([], 299)
    assert check(ROOT, "base", "head") == []
