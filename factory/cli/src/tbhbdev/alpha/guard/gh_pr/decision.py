# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Decide which GitHub pull request commands the hook refuses."""

import re
from typing import NamedTuple

from tbhbdev.alpha.guard.gh_options import (
    API_FIELD_FLAGS,
    API_VALUE_FLAGS,
    after_global_options,
)
from tbhbdev.alpha.guard.options import flag_values, split_args
from tbhbdev.alpha.guard.shell_commands import (
    program_of,
    simple_commands,
)

SUBCOMMANDS = ("create", "edit", "merge", "ready", "close")


GROUPS = {"pr": SUBCOMMANDS, "issue": ("close",), "run": ("rerun",)}


HINT_LINE = "tbhbdev hint:"


FOOTER = (
    "Use `tbhbdev alpha` for GitHub writes: `tbhbdev alpha --help` lists the "
    "commands, and `tbhbdev alpha session-context` reports this session's context."
)


BRANCH_REF = re.compile(r"(?:https?://[^/]+/)?/?repos/[^/]+/[^/]+/git/refs/heads/.+")


PR_NUMBER = re.compile(r"(?:\S*/pull/|#)?(\d+)")


DRAFT = ".github/PULL_REQUEST_DRAFT.md"


TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"


GH_RULE = (
    "An agent can't approve a pull request through gh, whoever opened it. "
    "Review runs post the approving review. "
    "Use a COMMENT review to leave feedback directly."
)


REVIEW_VALUE_FLAGS = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}


THREAD_MUTATION = re.compile(
    r"\bmutation\b.*?\b(resolve|unresolve)ReviewThread\s*\(", re.DOTALL
)


APPROVE_FIELD = re.compile(r"\s*event\s*=\s*(?i:approve)\s*")


REVIEWS = re.compile(
    r"(?:https?://[^/]+/)?/?repos/([^/]+)/([^/]+)/pulls/([^/]+)/reviews"
    r"(?:/[^/?#]+/events)?/?(?:\?.*)?"
)


class Match(NamedTuple):
    """A blocked `gh` call: the subcommand, the words after it, and its group."""

    sub: str
    args: list[str]
    group: str = "pr"


def deletes_branch(args: list[str]) -> bool:
    """Whether ``gh api`` args are a DELETE of a ``git/refs/heads/`` ref."""
    methods = flag_values(args, {"-X", "--method"}, API_VALUE_FLAGS)
    endpoints = split_args(args, API_VALUE_FLAGS)[1]
    return bool(
        methods
        and methods[-1].upper() == "DELETE"
        and endpoints
        and BRANCH_REF.fullmatch(endpoints[0])
    )


def gh_match(rest: list[str]) -> Match | None:
    """Match the words after ``gh``: global flags, then a blocked subcommand."""
    words = after_global_options(rest)
    if not words:
        return None
    group, *args = words
    if args and args[0] in GROUPS.get(group, ()):
        return Match(args[0], args[1:], group)
    if group == "api" and deletes_branch(args):
        return Match("delete-branch", args, group)
    if group == "api" and (thread := thread_mutation(args)):
        return Match(thread, [], "graphql")
    return None


def thread_mutation(args: list[str]) -> str | None:
    """The review command for a thread mutation in an inline query field."""
    endpoints = split_args(args, API_VALUE_FLAGS)[1]
    if not endpoints or endpoints[0] != "graphql":
        return None
    for value in flag_values(args, API_FIELD_FLAGS, API_VALUE_FLAGS):
        if value.startswith("query=") and (match := THREAD_MUTATION.search(value)):
            return match[1]
    return None


def find(text: str) -> Match | None:
    """The first direct `gh pr create|edit|merge` call in a shell command."""
    for _, words in simple_commands(text):
        found = gh_match(words[1:]) if program_of(words) == "gh" else None
        if found:
            return found
    return None


def approves(word: str) -> bool:
    """Whether a gh pr review flag word turns on ``--approve``."""
    if word == "--approve" or word.startswith("--approve="):
        return True
    if word.startswith("--"):
        return False
    for char in word[1:]:
        if char == "a":
            return True
        if f"-{char}" in REVIEW_VALUE_FLAGS:
            return False
    return False


def posts_approve(args: list[str]) -> bool:
    """Whether ``gh api`` args send a POST whose fields hold ``event=APPROVE``."""
    fields = flag_values(args, API_FIELD_FLAGS, API_VALUE_FLAGS)
    methods = flag_values(args, {"-X", "--method"}, API_VALUE_FLAGS)
    if methods and methods[-1].upper() != "POST":
        return False
    return any(APPROVE_FIELD.fullmatch(field) for field in fields)


def gh_approval(rest: list[str]) -> str | None:
    """The approval route described by the words after gh, if any."""
    rest = after_global_options(rest)
    if rest[:2] == ["pr", "review"]:
        flags, _ = split_args(rest[2:], REVIEW_VALUE_FLAGS)
        if any(approves(word) for word in flags):
            return "`gh pr review --approve`"
    if rest[:1] == ["api"]:
        args = rest[1:]
        endpoints = split_args(args, API_VALUE_FLAGS)[1]
        if endpoints and REVIEWS.fullmatch(endpoints[0]) and posts_approve(args):
            return "a `gh api` APPROVE review"
    return None


def approval_message(text: str) -> str | None:
    """The deny message for the first approval route in a shell command."""
    for _, words in simple_commands(text):
        program = program_of(words)
        route = None
        if program == "gh":
            route = gh_approval(words[1:])
        elif program.partition("-") == ("tbhb", "-", "approve"):
            route = "the retired approval command"
        if route:
            return f"{GH_RULE} The hook blocks {route}."
    return None


def other_hint(found: Match, number: str) -> str | None:
    """The block message for a close, rerun or branch delete; None for others."""
    if found.group == "api":
        return (
            "A `gh api` branch delete is blocked. For a closed or merged PR, run "
            f"`tbhbdev alpha pr delete-branch {number}` instead."
        )
    if found.group == "graphql":
        return (
            "A `gh api graphql` review thread mutation is blocked. From the repository "
            f"clone, run `tbhbdev alpha review {found.sub} <PR> <THREAD>`. Use "
            "`tbhbdev alpha review threads <PR>` to find the thread ID."
        )
    blocked = f"`gh {found.group} {found.sub}` is blocked."
    if found.group == "issue":
        return (
            f"{blocked} From the repository clone, run `tbhbdev alpha issue close "
            f"{number} --reason completed|not-planned [--comment TEXT]` (no --repo)."
        )
    if found.group == "run":
        return (
            f"{blocked} Run `tbhbdev alpha pr rerun N`, where N is the pull request "
            "number, to rerun the failed jobs of its latest run."
        )
    if found.sub == "close":
        return (
            f"{blocked} From the repository clone, run `tbhbdev alpha pr close "
            f"{number} --reason TEXT` (no --repo)."
        )
    return None


def hint(found: Match) -> str:
    """The block message: what was blocked and the command to run instead."""
    number = next((m[1] for w in found.args if (m := PR_NUMBER.fullmatch(w))), "<N>")
    other = other_hint(found, number)
    if other:
        return other
    blocked = f"`gh {found.group} {found.sub}` is blocked."
    if found.sub == "create":
        return (
            f"{blocked} Write {DRAFT} from {TEMPLATE}, check it with "
            "`tbhbdev alpha pr check`, then open the PR with `tbhbdev alpha pr create`."
        )
    if found.sub == "edit":
        return (
            f"{blocked} Update {DRAFT}, then run "
            f"`tbhbdev alpha pr edit {DRAFT} --pr {number}`."
        )
    if found.sub == "ready" and "--undo" in found.args:
        return (
            f"{blocked} `tbhbdev alpha pr ready` has no undo. Ask the operator to "
            "return the PR to draft."
        )
    if found.sub == "ready":
        return f"{blocked} Run `tbhbdev alpha pr ready {number}` instead."
    return (
        f"{blocked} Once the PR is approved, run `tbhbdev alpha merge {number}` "
        "(add `--dry-run` to preview the commit message first)."
    )


def repository_message(text: str, *, in_repository: bool) -> str | None:
    """The reason for a command restricted in a tbhb-dev repository."""
    found = find(text) if in_repository else None
    return f"{HINT_LINE}\n{hint(found)}\n{FOOTER}" if found else None
