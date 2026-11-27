"""Render peer-run briefs and check their fixed safeguards.

The checks use text patterns and recognize common prohibitions. Extra lines in
Hard rules are allowed. The checks catch omissions and common mistakes.
"""

import argparse
import re
import shlex
from collections import defaultdict
from pathlib import Path

from tbhbdev.alpha.agent.driver.decisions.prompt import (
    branch_warnings,
    check_hooks,
    normalize,
)

HOOK_RULE = (
    "Never run `prek install`, `pre-commit install`, `tbhbdev alpha install`, "
    "or any other hook installer."
)
STEP_BLOCK = """## Step 1: worktree first (required)

Before anything else, in the {repo} main checkout:

```sh
git fetch origin
git worktree add {path} -b {branch} {base}
cd {path}
```

If the worktree or branch already exists, stop and report.
In the main checkout, make NO commit, checkout, switch, reset, stash or file edit.
Do ALL further work in the new worktree.

```sh
uv sync  # Drop this line when the repo has no pyproject.toml.
bazelisk run //:bazel_env
```

{hook_rule}"""
COMMITS_BLOCK = (
    "Use plain `git commit` and let the hooks run. "
    "Never use `--no-verify`, `SKIP=`, `PREK_SKIP=`, `-c core.hooksPath=`, "
    "or any other skip or redirect of hooks. "
    "No `-s`, no Signed-off-by, no Co-authored-by."
)
PUSH_BLOCK = (
    "Before any `git push`, run the outgoing scan command below from the worktree. "
    "Stop and report on any nonzero exit. "
    "Never force-push an existing remote branch."
)
NO_PUSH_BLOCK = "Do NOT push and do NOT open a pull request."
HARD_RULES_BLOCK = """- After any denial, stop and report. No workaround by any tool, even an equivalent one.
- GitHub state comes only from awrel events. No gh polling, no sleep or pgrep wait loops.
- Never pkill, killall or kill by pattern; kill only a PID you started. Never connect to the `tmux -L agents` server or any agents socket.
- Never unset or export the 1Password service-account token variable. No `env`, `printenv`, `set` or `export -p` dumps; no credential helpers; don't read SSH keys, the Codex home, GitHub CLI config, or any token file.
- Never `rm`; move leftovers with `mv` into `$TMPDIR`. No ps, pgrep, top or sysctl; load checks use `uptime` only.
- No "core" or "shell" as a module or pattern name. Never write a public IP address. Agents don't open GitHub issues; follow-ups go in a private drafts file.
- Never bypass a guard or a hook. If a push is refused, push a new branch; never force-push.
- Touch only your worktree and `$TMPDIR`."""  # noqa: E501 -- fixed rule lines
REQUIRED_HEADINGS = (
    "## Step 1: worktree first (required)",
    "## Hard rules",
    "## Final report",
)
PROHIBITION_REPLACEMENT = ""
SPACE = " "
FORBIDDEN = (
    "prek install",
    "pre-commit install",
    "--no-verify",
    "SKIP=",
    "PREK_SKIP=",
    "core.hooksPath",
    "tmux -L agents",
    "follow-up issue",
    "gh issue create",
    "unset OP_",
    "env -u op_",
    "tbhbdev alpha install",
)
FORBIDDEN_REGEX = (
    ("git commit hook bypass", r"\bgit commit\b[^`;&|]*\s(?:-n\b|--no-ver[\w-]*)"),
    (
        "git push force option",
        r"\bgit push\b[^`;&|]*\s(?:--force\b(?!-with-lease\b)|-f\b)",
    ),
)
FIXED_BY_SECTION = {
    REQUIRED_HEADINGS[0]: (HOOK_RULE,),
    "## Commits": (COMMITS_BLOCK, NO_PUSH_BLOCK),
    "## Push": (PUSH_BLOCK,),
    "## Hard rules": tuple(HARD_RULES_BLOCK.splitlines()),
}
BUILD_HEADING = "## Build"
FIX_HEADING = "## Fix"
EXPECTED_SIZE = "Expected size (code lines)"
FAILING_LINE = "Failing file and line"


def render(  # noqa: PLR0913 -- Explicit template fields and worktree options.
    run: str,
    repo: str,
    branch: str | None = None,
    base: str = "origin/main",
    *,
    scan_command: str | None = None,
    kind: str = "build",
    expected_size: str = "",
    failing_line: str = "",
    proven_fix: str = "",
) -> str:
    """Return a skeleton with the fields for the selected brief kind."""
    step = STEP_BLOCK.format(
        repo=repo,
        path=shlex.quote(f".worktrees/{run}"),
        branch=shlex.quote(branch or f"agent/{run}"),
        base=shlex.quote(base),
        hook_rule=HOOK_RULE,
    )
    if kind == "fix":
        details = "\n\n".join(
            (
                FIX_HEADING,
                f"{FAILING_LINE}: {failing_line}".rstrip(),
                f"Proven fix: {proven_fix}".rstrip(),
            )
        )
    else:
        details = "\n\n".join(
            (
                BUILD_HEADING,
                f"{EXPECTED_SIZE}: {expected_size}".rstrip(),
                "Above 400 code lines, split the work into stacked PRs.",
            )
        )
    return "\n\n".join(
        (
            f"# {{TASK}} ({repo}, {run})",
            step,
            "## Task\n\n{TASK_DETAILS}",
            details,
            "## Gates\n\n{GATES}",
            f"## Commits\n\n{COMMITS_BLOCK}",
            f"## Push\n\n{PUSH_BLOCK}\nOutgoing scan command: `{scan_command}`"
            if scan_command is not None
            else NO_PUSH_BLOCK,
            f"## Hard rules\n\n{HARD_RULES_BLOCK}",
            "## Final report\n\n{FINAL_REPORT}\n",
        )
    )


def sections(text: str) -> dict[str, str]:
    """Collect second-level Markdown sections, ending at the next heading."""
    found: defaultdict[str, str] = defaultdict(str)
    heading = ""
    fence: str | None = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            delimiter = marker[1]
            if fence is None:
                fence = delimiter
            elif delimiter.startswith(fence):
                fence = None
        if fence is None and line.startswith("#"):
            heading = line
            found.setdefault(heading, "")
        else:
            found[heading] += line + "\n"
    return found


def instruction_text(found: dict[str, str]) -> str:
    """Normalize sections and remove their fixed safeguards before scanning."""
    remaining_sections = []
    for heading, content in found.items():
        body = normalize(content)
        for fixed in FIXED_BY_SECTION.get(heading, ()):
            body = body.replace(normalize(fixed), PROHIBITION_REPLACEMENT)
        remaining_sections.extend((normalize(heading), body))
    return SPACE.join(remaining_sections)


def check_push_section(content: str) -> list[str]:
    """Require the fixed push safeguard and one outgoing scan command line."""
    problems = []
    commands = re.findall(r"^Outgoing scan command: `(.*)`$", content, re.MULTILINE)
    if len(commands) != 1 or not re.fullmatch(r"[^`\r\n]+", commands[0]):
        problems.append("missing outgoing scan command")
    if normalize(PUSH_BLOCK) not in normalize(content):
        problems.append("missing push rule in Push section")
    return problems


def check_template_fields(found: dict[str, str]) -> list[str]:
    """Check required single-line fields in build and fix sections."""
    return [
        f"missing value for {field}"
        for heading, field in (
            (BUILD_HEADING, EXPECTED_SIZE),
            (FIX_HEADING, FAILING_LINE),
        )
        if heading in found
        and not re.search(
            rf"^{re.escape(field)}:[^\S\n]*\S", found[heading], re.MULTILINE
        )
    ]


def check(text: str) -> list[str]:
    """Return missing safeguards, placeholders and forbidden instructions."""
    found = sections(text)
    problems = [
        f"missing heading: {heading}"
        for heading in REQUIRED_HEADINGS
        if heading not in found
    ]
    problems.extend(check_template_fields(found))
    step = found[REQUIRED_HEADINGS[0]]
    if not any(
        re.match(r"^\s*(?:[-*+]\s+|\$ )?git worktree add \.worktrees/", line)
        for line in step.splitlines()
    ):
        problems.append("Step 1 lacks a git worktree add .worktrees/ line")
    push_rules = sum(
        normalize(text).count(normalize(rule)) for rule in (PUSH_BLOCK, NO_PUSH_BLOCK)
    )
    if push_rules == 0:
        problems.append("missing push rule")
    elif push_rules != 1:
        problems.append("expected exactly one push rule")
    if "## Push" in found:
        problems.extend(check_push_section(found["## Push"]))
    rules = normalize(found["## Hard rules"])
    problems.extend(
        f"missing hard rule: {rule}"
        for rule in HARD_RULES_BLOCK.splitlines()
        if normalize(rule) not in rules
    )
    problems.extend(
        f"unfilled placeholder: {placeholder}"
        for placeholder in re.findall(r"(?<!\$)\{[A-Z][A-Z0-9_]*\}", text)
    )
    remaining = instruction_text(found)
    problems.extend(
        f"forbidden instruction: {forbidden}"
        for forbidden in FORBIDDEN
        if normalize(forbidden) in remaining
    )
    problems.extend(
        f"forbidden instruction: {label}"
        for label, pattern in FORBIDDEN_REGEX
        if re.search(normalize(pattern), remaining)
    )
    problems.extend(check_hooks(text))
    return problems


def check_files(files: list[str]) -> int:
    status = 0
    for filename in files:
        try:
            text = Path(filename).read_bytes().decode()
        except (OSError, UnicodeError) as exc:
            problems = [str(exc)]
        else:
            problems = check(text)
            for warning in branch_warnings(text):
                print(f"warning: {filename}: {warning}")
        if problems:
            status = 1
            for problem in problems:
                print(f"{filename}: {problem}")
        else:
            print(f"OK: {filename}")
    return status


def field_value(value: str) -> str:
    """Read a populated single-line template field."""
    if not value.strip() or value.splitlines() != [value]:
        msg = "expected a non-empty single-line value"
        raise argparse.ArgumentTypeError(msg)
    return value


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="write a peer-run brief skeleton")
    new.add_argument("run", metavar="RUN")
    new.add_argument("--repo", required=True, metavar="OWNER/REPO")
    new.add_argument("--branch")
    new.add_argument("--base", default="origin/main", metavar="REF")
    new.add_argument("--push", action="store_true")
    new.add_argument("--scan-command", metavar="CMD")
    new.add_argument("--kind", choices=("build", "fix"), default="build")
    new.add_argument(
        "--expected-size", type=field_value, metavar="LINES", help="build code estimate"
    )
    new.add_argument(
        "--failing-line",
        type=field_value,
        metavar="FILE:LINE",
        help="fix failure location",
    )
    new.add_argument("--proven-fix", type=field_value, help="fix evidence")
    new.add_argument("-o", metavar="FILE")
    lint = commands.add_parser("check", help="check completed peer-run briefs")
    lint.add_argument("files", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check_files(args.files)
    if args.push != (args.scan_command is not None):
        parser.error("--push and --scan-command must be used together")
    if args.kind == "fix" and args.expected_size is not None:
        parser.error("--expected-size is only for build briefs")
    if args.kind == "build" and (args.failing_line or args.proven_fix):
        parser.error("--failing-line and --proven-fix are only for fix briefs")
    for value, pattern, label in (
        (args.run, r"[A-Za-z0-9][A-Za-z0-9._-]*", "RUN"),
        (args.repo, r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", "REPO"),
        (args.branch, r"[^\s{}]+", "BRANCH"),
        (args.base, r"[^\s{}]+", "BASE"),
        (args.scan_command, r"[^`\r\n]+", "SCAN COMMAND"),
    ):
        if value is not None and not re.fullmatch(pattern, value):
            parser.error(f"invalid {label}: {value!r}")
    text = render(
        args.run,
        args.repo,
        args.branch,
        args.base,
        scan_command=args.scan_command,
        kind=args.kind,
        expected_size=args.expected_size or "",
        failing_line=args.failing_line or "",
        proven_fix=args.proven_fix or "",
    )
    if args.o:
        try:
            Path(args.o).write_bytes(text.encode())
        except OSError as exc:
            parser.error(str(exc))
    else:
        print(text, end="")
    return 0
