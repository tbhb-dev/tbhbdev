# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request baseline."""

import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING, cast

import pytest
from fake_tool import (
    pythonpath,
)
from jsonschema import Draft202012Validator
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_test_support import (
    SRC,
    TEMPLATE,
    World,
    run_main,
    write_draft,
)

from tbhbdev.alpha.pull_request import baseline as pr_baseline
from tbhbdev.alpha.pull_request import body as pr_body
from tbhbdev.alpha.pull_request.draft import DRAFT

if TYPE_CHECKING:
    from pathlib import Path


host_names = pr_test_support.host_names
world = pr_test_support.world


def test_sections_file_matches_template_guidance() -> None:
    sections = pr_baseline.load_sections()
    template = TEMPLATE.read_text()
    headings = re.findall(r"^## (.+)$", template, re.MULTILINE)
    assert headings == [section.heading for section in sections]
    guidance = re.findall(
        r"^## .+\n\n<!-- (Required|Optional), at most (\d+) words",
        template,
        re.MULTILINE,
    )
    assert [(g[0] == "Required", int(g[1])) for g in guidance] == [
        (section.required, section.max_words) for section in sections
    ]


def test_template_has_no_provenance_section() -> None:
    assert "Provenance" not in TEMPLATE.read_text().split("## Why")[1]


def test_limits() -> None:
    sections = pr_baseline.load_sections()
    assert {s.heading: s.max_words for s in sections} == {
        "Why": 60,
        "Behavior change": 120,
        "Decisions": 120,
        "Deviations from the task": 80,
        "Needs human judgment": 100,
        "Risk": 80,
        "Verification": 150,
        "Human steering": 40,
    }
    assert [s.heading for s in sections if s.required] == [
        "Why",
        "Behavior change",
        "Verification",
    ]


def test_schema_is_valid_and_closed() -> None:
    schema = pr_baseline.load_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    properties = cast("dict[str, dict[str, str]]", schema["properties"])
    assert all(prop.get("description") for prop in properties.values())


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"base": "release/1.x", "draft": True},
        {"labels": ["bug", "good first issue"], "reviewers": ["tbhb", "org/team"]},
        {"assignees": ["@me", "tbhb"], "milestone": "v1 beta"},
    ],
)
def test_schema_accepts(meta: dict[str, object]) -> None:
    assert Draft202012Validator(pr_baseline.load_schema()).is_valid(meta)


@pytest.mark.parametrize(
    "meta",
    [
        {"title": "x"},
        {"base": ""},
        {"base": "-x"},
        {"draft": "yes"},
        {"labels": ["a,b"]},
        {"labels": [" a"]},
        {"labels": ["a", "a"]},
        {"labels": ["x" * 51]},
        {"reviewers": ["bad name"]},
        {"reviewers": ["-x"]},
        {"assignees": ["@you"]},
        {"milestone": " v1"},
        {"milestone": ""},
    ],
)
def test_schema_rejects(meta: dict[str, object]) -> None:
    assert not Draft202012Validator(pr_baseline.load_schema()).is_valid(meta)


def test_sections_without_a_word_limit(world: World) -> None:
    (world.root / pr_baseline.SECTIONS).write_text(
        '[[section]]\nheading = "A"\nrequired = true\n\n'
        '[[section]]\nheading = "B"\nrequired = false\nmax_words = 5\n'
    )
    assert pr_baseline.load_sections() == [
        pr_body.Section("A", required=True, max_words=None),
        pr_body.Section("B", required=False, max_words=5),
    ]


@pytest.mark.parametrize("missing", [pr_baseline.SCHEMA, pr_baseline.SECTIONS])
@pytest.mark.parametrize(
    "argv",
    [["check"], ["create", "--dry-run"], ["edit", str(DRAFT), "--pr", "3"]],
)
def test_a_missing_baseline_file_names_it(
    world: World, missing: Path, argv: list[str]
) -> None:
    write_draft(world)
    (world.root / missing).rename(world.root.parent / missing.name)
    with pytest.raises(SystemExit) as info:
        run_main(*argv)
    assert info.value.code == (
        f"{missing} is missing: this repository needs the shared baseline"
        " from tbhb-dev/rules"
    )


def test_a_missing_baseline_file_exits_without_a_traceback(world: World) -> None:
    write_draft(world)
    (world.root / pr_baseline.SECTIONS).rename(world.root.parent / "sections.toml")
    env = {**os.environ, "PYTHONPATH": pythonpath(SRC)}
    env.pop("MUTANT_UNDER_TEST", None)
    done = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "alpha", "pr", "check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=world.root,
        env=env,
    )
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == (
        f"{pr_baseline.SECTIONS} is missing: this repository needs the shared baseline"
        " from tbhb-dev/rules\n"
    )
