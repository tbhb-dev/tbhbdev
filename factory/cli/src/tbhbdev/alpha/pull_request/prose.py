# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Run the prose linters and synchronize missing Vale styles."""

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from tbhbdev.alpha.pull_request.body import Problem
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.draft import DRAFT
from tbhbdev.alpha.pull_request.prose_output import (
    CSPELL_LINE,
    RUMDL_LINE,
    lint_findings,
    parse_lines,
    parse_styles,
    parse_vale,
    title_findings,
)
from tbhbdev.alpha.pull_request.tools import BAZEL, COG

# Config files each linter reads, mirrored into the temp tree by symlink.
CONFIGS = (".vale.ini", ".vale", ".cspell.jsonc", ".cspell-words.txt", ".rumdl.toml")


# A merge appends " (#N)" and the header must fit 72; 64 leaves room
# for a pull request number of up to four digits.
MAX_TITLE = 72 - len(" (#9999)")


# Module constants, not literals in the functions: macOS file systems ignore
# case, so a mutant that changes a path's case there would pass every test.
VALE_INI = ".vale.ini"
STYLES = ".vale"
VALE_SYNC = "//:vale_sync"


# The title is a commit header, so the sentence-case heading rule does not apply
# to line 1: a finding on it reads (1, "vale project.HeadingTitleCase: ...").
TITLE_ALERT = (1, "vale project.HeadingTitleCase")


def title_problems(title: str, limit: int = MAX_TITLE) -> list[str]:
    """Why a title is not a Conventional Commit header that fits limit."""
    done = pr_tools.run(
        [pr_tools.find_tool(COG), "verify", "--file", "-"],
        cwd=pr_tools.root(),
        stdin=title,
    )
    return title_findings(title, limit, done.returncode, done.stderr)


LINTERS: tuple[tuple[str, tuple[str, ...], Callable[[str], list[Problem]]], ...] = (
    ("vale", ("--output=JSON",), parse_vale),
    (
        "cspell",
        ("--config", ".cspell.jsonc", "--no-summary", "--no-progress"),
        parse_lines(CSPELL_LINE, "cspell"),
    ),
    ("rumdl", ("check",), parse_lines(RUMDL_LINE, "rumdl")),
)


def vale_styles(top: Path) -> list[str]:
    """Read the styles named in the repository's Vale configuration."""
    return parse_styles((top / VALE_INI).read_text())


def missing_styles(top: Path) -> list[str]:
    """The named styles with no directory under .vale/."""
    return [name for name in vale_styles(top) if not (top / STYLES / name).is_dir()]


def style_problem(top: Path) -> str | None:
    """Sync missing vale styles once; why vale can't run, or None."""
    missing = missing_styles(top)
    if not missing:
        return None
    bazel = pr_tools.find_tool(BAZEL)
    target = pr_tools.run([bazel, "query", VALE_SYNC], cwd=top)
    if target.returncode != 0 or VALE_SYNC not in target.stdout.splitlines():
        return (
            f"vale styles missing from .vale/: {', '.join(missing)}; "
            f"missing Bazel target {VALE_SYNC}"
        )
    done = pr_tools.run([bazel, "run", VALE_SYNC], cwd=top)
    still = missing_styles(top)
    if not still:
        return None
    why = " ".join((done.stderr or done.stdout).split())
    return (
        f"vale styles missing from .vale/: {', '.join(still)}; "
        f"`{BAZEL} run {VALE_SYNC}` exited {done.returncode}: {why}"
    )


def lint(
    rendered: Sequence[str],
    first: int,
    *,
    path: Path = DRAFT,
    drop: frozenset[tuple[int, str]] = frozenset({TITLE_ALERT}),
    vale_only: bool = False,
) -> list[Problem]:
    """Lint the title and description with the repository's own configs.

    The text goes into a temp tree with the draft's relative path and symlinks
    to the configs, so the globs in those configs match. Rendered line N is
    draft line ``first + N - 1``. ``drop`` matches a line and either a rule
    prefix or its full diagnostic.
    """
    if vale_only:
        configs = (VALE_INI, STYLES)
        linters = (("vale", ("--output=JSON", "--minAlertLevel=error"), parse_vale),)
    else:
        configs, linters = CONFIGS, LINTERS
    top = pr_tools.root()
    for name in configs:
        if not (top / name).exists():
            message = f"missing lint config {name} in {top}"
            raise SystemExit(message)
    styles = style_problem(top)
    problems: list[Problem] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name in configs:
            (work / name).symlink_to(top / name)
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text("\n".join(rendered) + "\n")
        for name, args, parse in linters:
            if name == "vale" and styles:
                problems.append((first, f"vale: {styles}"))
                continue
            problems.extend(
                (first + line - 1, text)
                for line, text in run_linter(
                    name, [pr_tools.find_tool(name), *args], parse, work, path
                )
                if (line, text.split(":")[0]) not in drop and (line, text) not in drop
            )
    return problems


def run_linter(
    name: str,
    argv: list[str],
    parse: Callable[[str], list[Problem]],
    work: Path,
    path: Path = DRAFT,
) -> list[Problem]:
    """Run one linter on ``path`` in ``work``; its findings, or why it failed."""
    done = pr_tools.run([*argv, str(path)], cwd=work)
    return lint_findings(name, parse, done.returncode, done.stdout, done.stderr)
