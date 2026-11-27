"""Validate launch briefs and schemas."""

from __future__ import annotations

import json
import re
import unicodedata
from itertools import pairwise
from typing import TYPE_CHECKING

from tbhbdev.alpha import brief_text
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.driver.decisions.credentials import check_secret
from tbhbdev.alpha.agent.model import Invalid, Profile
from tbhbdev.alpha.brief_text import PROSE_BREAK, normalize, prohibiting

if TYPE_CHECKING:
    import argparse


COMMAND_ARGS = r"[^`\n;&|]*"

ARG_END = r"(?=$|[ \n`;&|\"'(),]|\.(?=$|[ \n]))"

TRAILER = r"(?:generated-by|assisted-by)"

COMMAND_CONFLICTS = (
    (
        (
            r"\bgit merge(?![-\w])"
            rf"(?!{COMMAND_ARGS} --(?:no-commit|squash|ff-only|abort|continue|quit)"
            rf"{ARG_END})"
        ),
        (
            "merge commits lack the required Generated-by trailer. "
            "Use git merge --no-commit, then a plain git commit"
        ),
    ),
    (
        (
            r"\bgit commit(?![-\w])"
            rf"(?={COMMAND_ARGS} (?:-[mf]|--(?:message|file)(?=[ =])))"
            rf"{COMMAND_ARGS}\b{TRAILER}:"
        ),
        "leave hand-written identity trailers out of the commit message",
    ),
    (rf"\bgit commit(?![-\w]){COMMAND_ARGS} -n{ARG_END}", "hook bypass is forbidden"),
    (
        (
            rf"\bgit push(?![-\w]){COMMAND_ARGS} [\"']?\+?(?:head:)?"
            rf"(?:refs/heads/)?(?:main|master){ARG_END}"
        ),
        "push to main or master is forbidden",
    ),
)

PROSE_CONFLICTS = (
    (
        (
            r"\b(?:no|without|omit|strip|remove|drop|skip|exclude|leave out|"
            r"(?:don['\u2019]t|do not|never) (?:add|include|stamp|write)) "
            rf"(?:an? |the |any )?[`\"']?{TRAILER}(?![-\w])"
        ),
        "let the hooks stamp the identity trailers",
    ),
    (r"(?<![\w-])--no-verify(?![-\w])", "hook bypass is forbidden"),
    (r"(?<![\w-])(?:prek|pre-commit) install(?![-\w])", "hook install is forbidden"),
)

BRANCH_INSTRUCTION = re.compile(
    r"(?:\bgit (?:checkout -[bB]|switch(?: -[cC])?|branch) "
    rf"|\b(?P<checkout>git checkout)(?!{COMMAND_ARGS} --{ARG_END}) "
    rf"|\bgit worktree add{COMMAND_ARGS}? -[bB] "
    r"|\b(?:create|switch to|check out|commit on|work on) "
    r"(?:a |the )?branch (?:named )?)"
    r"[`\"']?(?P<target>[^\s`\"';&|<>()]+)",
    re.IGNORECASE,
)

# A period can be part of a branch name or punctuation after a ref.
REF_START = r"(?<![\w./@+-])"

REF_END = r"(?=\.*(?:$|[\s`\"'(),;:<>\]]))"

BRANCH_REF = r"[A-Za-z0-9._/-]*[A-Za-z0-9_/-]"

SHORT_AGENT_REF = rf"agent/{BRANCH_REF}{REF_END}"

AGENT_BRANCH_MENTION = re.compile(rf"{REF_START}{SHORT_AGENT_REF}")

_PROMPT_REF = re.compile(
    rf"{REF_START}refs/(?:pull/[0-9]+/head|heads/{BRANCH_REF}){REF_END}"
)

_REF_USE = re.compile(
    r"\b(?:(?i:use|reuse|fetch|ref|start from|check out|switch to"
    r"|git (?:checkout|switch|show|merge|rebase)) "
    r"|(?i:git fetch) \S+ )[\[(`\"']?"
    rf"({SHORT_AGENT_REF})"
)

_UPDATE = re.compile(
    r"\b(?:(?i:update|push (?:\w+ )?to) "
    r"(?:(?i:a|the|this|its|current|existing|branch) )*"
    r"|(?i:git push) [^\s`]+ (?:[^\s`:]+:)?)"
    rf"(?:[`\"']?(refs/heads/{BRANCH_REF}{REF_END}|{SHORT_AGENT_REF})"
    r"|(?i:(?:PR|pull request)(?: #\d+)? (?:head|branch))\b)"
)

_PUSH_COMMAND = re.compile(
    r"\bgit(?: -c (?:'[^'\n]+'|\"[^\"\n]+\"|[^\s`]+))* push "
    r"(?:-[^\s`;&|]+ )*[^-\s`;&|]+ (?:-[^\s`;&|]+ )*"
    rf"[\"']?\+?(?:[^\s:`\"']+:)?({BRANCH_REF}){REF_END}"
)
_PUSH_PROSE = re.compile(
    r"(?<!git )\bpush\s+(?:(?:\w+\s+)?to\s+)?"
    r"(?:(?:a|the|this|its|current|existing)\s+)*(?:branch\s+(?:named\s+)?)?[`\"']?"
    rf"({BRANCH_REF}){REF_END}",
    re.IGNORECASE,
)


def prompt_ref_warnings(
    prompt: str, fetch_refs: list[str], malformed_refs: tuple[str, ...]
) -> list[str]:
    """Report malformed refs and valid refs missing from the fetch list."""
    warnings = [f"malformed ref {ref!r} in the prompt" for ref in malformed_refs]
    warnings.extend(
        f"the prompt names {ref} but this launch has no --fetch-ref {ref}; "
        "a run whose mirror lacks it may end with no work"
        for ref in dict.fromkeys(_PROMPT_REF.findall(prompt))
        if ref not in fetch_refs and request.valid_fetch_ref(ref)
    )
    return warnings


def malformed_prompt_refs(prompt: str) -> tuple[str, ...]:
    """Return malformed full ref names recognized in the prompt."""
    return tuple(
        ref
        for ref in dict.fromkeys(_PROMPT_REF.findall(prompt))
        if not request.valid_fetch_ref(ref)
    )


def check_hooks(text: str) -> list[str]:
    """Return hook conflicts in a free-form brief."""
    lines = "\n".join(normalize(line) for line in text.splitlines())
    # Prose can wrap within a prohibition. Shell commands end at a newline.
    prose = "\n\n".join(normalize(block) for block in PROSE_BREAK.split(lines))
    return [
        f"hook conflict: {reason}"
        for content, rules in ((lines, COMMAND_CONFLICTS), (prose, PROSE_CONFLICTS))
        for pattern, reason in rules
        for match in re.finditer(pattern, content)
        if not prohibiting(content, match.start())
    ]


def looks_like_branch(target: str) -> bool:
    """Recognize qualified refs and conventional unqualified branch names."""
    return not target.startswith("-") and (
        "/" in target
        or target in {"main", "master"}
        or target.startswith(brief_text.UNQUALIFIED_BRANCH_PREFIXES)
    )


def branch_instructions(text: str) -> list[str]:
    """Return branch names from affirmative branch selection instructions."""
    text = "\n".join(
        " ".join(line.split())
        for line in unicodedata.normalize("NFKC", text).splitlines()
    )
    branches = []
    for match in BRANCH_INSTRUCTION.finditer(text):
        branch = match["target"].rstrip(".,")
        if (
            looks_like_branch(branch)
            and not (match["checkout"] and brief_text.looks_like_checkout_path(branch))
            and not prohibiting(text, *match.span())
        ):
            branches.append(branch)
    return branches


def branch_warnings(
    text: str,
    run: str | None = None,
    *,
    fetch_refs: tuple[str, ...] = (),
    ignored_refs: tuple[str, ...] = (),
) -> list[str]:
    """Warn about foreign branch instructions and unfetched agent branch mentions."""
    allowed = "agent/" if run is None else f"agent/{run}"
    branches = branch_instructions(text)
    if run is not None:
        branches.extend(
            branch
            for branch in AGENT_BRANCH_MENTION.findall(
                unicodedata.normalize("NFKC", text)
            )
            if f"refs/heads/{branch}" not in fetch_refs
        )
    warnings = []
    for branch in dict.fromkeys(branches):
        name = branch.removeprefix("refs/heads/")
        allowed_branch = name.startswith(allowed) if run is None else name == allowed
        if (
            allowed_branch
            or branch in ignored_refs
            or f"refs/heads/{name}" in ignored_refs
        ):
            continue
        warnings.append(f"use the launcher branch {allowed} instead of {branch}")
    return warnings


def _instructions(pattern: re.Pattern[str], prose: str) -> list[str | None]:
    return [
        match[1]
        for match in pattern.finditer(prose)
        if not prohibiting(prose, *match.span())
    ]


def _prose_push_targets(prompt: str) -> list[str]:
    breaks = (match.end() for match in PROSE_BREAK.finditer(prompt))
    return [
        match[1]
        for start, end in pairwise((0, *breaks, len(prompt)))
        for match in _PUSH_PROSE.finditer(prompt, start, end)
        if looks_like_branch(match[1]) and not prohibiting(prompt, *match.span())
    ]


def _unfetched_explicit_refs(
    prompt: str, named_refs: list[str | None], fetch_refs: list[str]
) -> list[str]:
    explicit = [
        ref
        for ref in _PROMPT_REF.findall(prompt)
        if ref.startswith(("refs/pull/", "refs/heads/agent/"))
    ]
    explicit.extend(
        f"refs/heads/{ref}" for ref in named_refs if ref and ref.startswith("agent/")
    )
    return [
        ref
        for ref in dict.fromkeys(explicit)
        if ref not in fetch_refs and request.valid_fetch_ref(ref)
    ]


def _wrong_build_branches(
    prompt: str,
    branch: str,
    start_ref: str | None,
    updates: list[str | None],
) -> list[str]:
    selected = [
        target
        for target in branch_instructions(prompt)
        if target.removeprefix("refs/heads/") != branch
    ]
    push_targets = _instructions(_PUSH_COMMAND, prompt) + _prose_push_targets(prompt)
    allowed_pushes = {branch, "HEAD"}
    # Keep the existing --start-ref guidance for recognized PR updates.
    update_refs = updates if start_ref is None else [start_ref]
    allowed_pushes.update(ref.removeprefix("refs/heads/") for ref in update_refs if ref)
    selected.extend(
        target
        for target in push_targets
        if target and target.removeprefix("refs/heads/") not in allowed_pushes
    )
    return [
        f"the brief directs work to {target}. Use the launcher branch {branch}."
        for target in dict.fromkeys(selected)
        if not target.startswith("refs/heads/") or request.valid_fetch_ref(target)
    ]


def prompt_launch_problems(
    prompt: str,
    fetch_refs: list[str],
    start_ref: str | None,
    *,
    build_branch: str | None,
) -> list[str]:
    """Check explicit ref uses and branch update instructions in a launch brief."""
    # Fold line wraps within a paragraph without changing ref case.
    prose = "\n\n".join(" ".join(block.split()) for block in PROSE_BREAK.split(prompt))
    updates = [
        target
        for target in _instructions(_UPDATE, prose)
        if target is None
        or request.valid_fetch_ref(f"refs/heads/{target.removeprefix('refs/heads/')}")
    ]
    named_refs = _instructions(_REF_USE, prose) + updates
    if build_branch is not None:
        own_refs = (build_branch, f"refs/heads/{build_branch}")
        updates = [ref for ref in updates if ref not in own_refs]
        fetch_refs = [*fetch_refs, own_refs[1]]
    problems = [
        f"the brief names {ref}. Add `--fetch-ref {ref}`"
        for ref in _unfetched_explicit_refs(prompt, named_refs, fetch_refs)
    ]
    if build_branch is None:
        return problems
    problems.extend(_wrong_build_branches(prompt, build_branch, start_ref, updates))
    if start_ref is None and updates:
        target = updates[0] or "<branch>"
        ref = f"refs/heads/{target.removeprefix('refs/heads/')}"
        problems.append(
            "the brief asks to update a PR branch. "
            f"Use `--fetch-ref {ref} --start-ref {ref}`"
        )
    return problems


def check_schema(schema: str) -> None:
    check_secret(schema, "schema")
    json.loads(schema)


def check_brief(prompt: str, args: argparse.Namespace) -> list[str]:
    check_secret(prompt, "brief")
    pinned_refs = (args.ref,) if args.ref and args.ref.startswith("refs/") else ()
    available_refs = [*args.fetch_ref, *pinned_refs]
    profile = Profile(args.profile)
    build_branch = f"agent/{args.name}" if profile is Profile.BUILD else None
    problems = check_hooks(prompt) + prompt_launch_problems(
        prompt,
        available_refs,
        args.start_ref,
        build_branch=build_branch,
    )
    if problems:
        raise Invalid(
            "\n".join(f"{args.prompt_file}: {problem}" for problem in problems)
        )
    malformed_refs = malformed_prompt_refs(prompt)
    warnings = branch_warnings(
        prompt,
        run=args.name,
        fetch_refs=tuple(args.fetch_ref),
        ignored_refs=(*pinned_refs, *malformed_refs),
    )
    warnings.extend(prompt_ref_warnings(prompt, available_refs, malformed_refs))
    return warnings
