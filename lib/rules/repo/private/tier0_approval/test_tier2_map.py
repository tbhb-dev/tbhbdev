# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import runpy
import sys
from pathlib import Path

import pytest

from repo.private.tier0_approval import __main__ as gate
from repo.private.tier0_approval import tier2_map

SCRIPT = Path(tier2_map.__file__)
# The root AGENTS.md sits above the lib/rules module. A Bazel run stages it at
# the root of the copied tree beside the module's MODULE.bazel, so this finds it
# there and in a full checkout alike, and fails at collection where it is absent.
ROOT_AGENTS = next(
    parent / "AGENTS.md"
    for parent in SCRIPT.parents
    if (parent / "AGENTS.md").is_file() and (parent / "MODULE.bazel").is_file()
)
SECTION = """\
# Agent instructions

Words in `backticks` and a table of `other` things.

| Name | Note |
| --- | --- |
| `not/a/path` | Outside the section. |

## Tier 2 paths

Two fresh runs.

| Glob | Why |
| --- | --- |
| `.github/workflows/**` | Workflows. |
| `lib/rules/**` | Rules, with a `quoted` word. |
| `AGENTS.md` | This file. |

## Commands

| Glob | Why |
| --- | --- |
| `after/the/section` | Not in the map. |
"""


def test_tier2_paths_reads_the_table_in_order() -> None:
    expected = ".github/workflows/**\nlib/rules/**\nAGENTS.md"
    assert tier2_map.tier2_paths(SECTION) == expected


@pytest.mark.parametrize("text", ["", "# Title\n\n| `a` | b |\n", "## Tier 2 paths"])
def test_tier2_paths_needs_the_heading_and_rows(text: str) -> None:
    with pytest.raises(ValueError, match=r"[Tt]ier 2"):
        tier2_map.tier2_paths(text)


def test_tier2_paths_rejects_an_empty_table() -> None:
    text = "## Tier 2 paths\n\n| Glob | Why |\n| --- | --- |\n\n## Next\n| `a` | b |\n"
    with pytest.raises(ValueError, match="no rows"):
        tier2_map.tier2_paths(text)


def test_real_map_lists_the_gate_paths() -> None:
    paths = tier2_map.tier2_paths(ROOT_AGENTS.read_text(encoding="utf-8")).splitlines()
    assert {
        ".github/workflows/**",
        "lib/rules/repo/private/tier0_approval/**",
        "factory/cli/src/tbhbdev/merge/**",
        "factory/cli/src/tbhbdev/alpha/review_data/**",
        "factory/cli/src/tbhbdev/alpha/review_prompt_data/**",
    } <= set(paths)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".github/workflows/ci.yml", True),
        ("factory/cli/src/tbhbdev/merge/github.py", True),
        (
            "factory/cli/src/tbhbdev/alpha/review_data/PULL_REQUEST_REVIEW_CHECKLIST.md",
            True,
        ),
        ("factory/cli/src/tbhbdev/alpha/review_prompt_data/tier-2-a.txt", True),
        ("lib/rules/repo/private/tier0_approval/__main__.py", True),
        ("docs/README.md", False),
    ],
)
def test_real_map_matches(path: str, expected: bool) -> None:
    text = ROOT_AGENTS.read_text(encoding="utf-8")
    assert gate.matches(path, tier2_map.tier2_paths(text)) is expected


def test_main_prints_the_map(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text(SECTION, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(agents)])
    monkeypatch.delitem(sys.modules, "repo.private.tier0_approval.tier2_map")
    runpy.run_path(str(SCRIPT), run_name="__main__")
    assert capsys.readouterr().out == ".github/workflows/**\nlib/rules/**\nAGENTS.md\n"
