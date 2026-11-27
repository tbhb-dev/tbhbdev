# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request draft io."""

from typing import TYPE_CHECKING

from unit.alpha.pull_request.pr_draft_samples import structure
from unit.alpha.pull_request.pr_test_support import FIXTURE, World, run_main

from tbhbdev.alpha.identity.commit.grammar import LINE
from tbhbdev.alpha.pull_request import body as pr_body
from tbhbdev.alpha.pull_request import draft_io as pr_draft_io
from tbhbdev.alpha.pull_request import frontmatter

if TYPE_CHECKING:
    import pytest

from unit.alpha.pull_request import pr_test_support

draft_world = pr_test_support.draft_world
host_names = pr_test_support.host_names
world = pr_test_support.world


def test_fixture_structure_passes() -> None:
    assert structure(FIXTURE.read_text()) == []


def test_check_reports_frontmatter_syntax_errors() -> None:
    assert pr_draft_io.check_text("---\nbase: main\n").problems == [
        (1, "frontmatter: unterminated frontmatter"),
    ]


def lint_checked(world: World, text: str) -> list[pr_body.Problem]:
    del world
    return pr_draft_io.check_text(text).problems


def test_lint_skipped_without_a_title(draft_world: World) -> None:
    assert lint_checked(draft_world, "## Why\n\nw\n")
    assert draft_world.calls("vale") == []
    assert draft_world.calls("cog") == []


def test_create_with_an_unreadable_path(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main("create", str(world.root)) == 1
    assert "cannot read the draft: Is a directory" in capsys.readouterr().out


def test_the_passing_draft_has_no_bare_references() -> None:
    _, body = frontmatter.parse_frontmatter(FIXTURE.read_text())
    scanned, _ = pr_body.scan(body.splitlines(), 1)
    assert pr_body.ref_problems(scanned) == []


def test_the_passing_draft_has_no_wrapped_paragraphs() -> None:
    _, body = frontmatter.parse_frontmatter(FIXTURE.read_text())
    scanned, _ = pr_body.scan(body.splitlines(), 1)
    assert pr_body.wrap_problems(scanned, LINE) == []
