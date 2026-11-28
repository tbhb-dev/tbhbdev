# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the pull request command and approval decisions."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tbhbdev.alpha.guard.gh_pr import decision as guard

CREATE = (
    "`gh pr create` is blocked. Write .github/PULL_REQUEST_DRAFT.md from "
    ".github/PULL_REQUEST_TEMPLATE.md, check it with `tbhbdev alpha pr check`, "
    "then open the PR with `tbhbdev alpha pr create`."
)


EDIT = (
    "`gh pr edit` is blocked. Update .github/PULL_REQUEST_DRAFT.md, "
    "then run `tbhbdev alpha pr edit .github/PULL_REQUEST_DRAFT.md --pr {n}`."
)


READY = "`gh pr ready` is blocked. Run `tbhbdev alpha pr ready {n}` instead."


PR_CLOSE = (
    "`gh pr close` is blocked. From the repository clone, run "
    "`tbhbdev alpha pr close {n} --reason TEXT` (no --repo)."
)


ISSUE_CLOSE = (
    "`gh issue close` is blocked. From the repository clone, run "
    "`tbhbdev alpha issue close {n} --reason completed|not-planned "
    "[--comment TEXT]` (no --repo)."
)


RERUN = (
    "`gh run rerun` is blocked. Run `tbhbdev alpha pr rerun N`, where N is the "
    "pull request number, to rerun the failed jobs of its latest run."
)


DELETE_BRANCH = (
    "A `gh api` branch delete is blocked. For a closed or merged PR, run "
    "`tbhbdev alpha pr delete-branch {n}` instead."
)


THREAD_HINT = (
    "A `gh api graphql` review thread mutation is blocked. From the repository "
    "clone, run `tbhbdev alpha review {command} <PR> <THREAD>`. Use "
    "`tbhbdev alpha review threads <PR>` to find the thread ID."
)


MERGE = (
    "`gh pr merge` is blocked. Once the PR is approved, run "
    "`tbhbdev alpha merge {n}` (add `--dry-run` to preview the commit message first)."
)


BLOCKED = [
    "gh pr create",
    "gh pr create --title x --body y",
    "gh pr edit 3",
    "gh pr merge 9 --squash",
    "  gh   pr   merge 9",
    "GH_CONFIG_DIR=x gh pr merge 1",
    "FOO=1 BAR='a b' gh pr merge 1",
    "env FOO=1 gh pr create",
    "env -i gh pr create",
    "command gh pr merge 1",
    "exec gh pr merge 1",
    "sudo -n gh pr merge 1",
    "nohup gh pr merge 1",
    "time gh pr merge 1",
    "/opt/homebrew/bin/gh pr merge 1",
    "cd a && gh pr merge 1",
    "cd a; gh pr merge 1",
    "false || gh pr merge 1",
    "echo x | gh pr merge 1",
    "echo x & gh pr merge 1",
    "(gh pr merge 5)",
    "( cd a && gh pr merge 5 )",
    "{ gh pr merge 5; }",
    "x=$(gh pr create)",
    'echo "$(gh pr merge 1)"',
    'echo "a $(echo b) $(gh pr merge 1)"',
    "echo `gh pr merge 1`",
    'echo "`gh pr merge 1`"',
    "echo $(echo $(gh pr merge 1))",
    "gh -R a/b pr edit 3",
    "gh --repo a/b pr edit 3",
    "gh --repo=a/b pr edit 3",
    "gh --hostname h pr edit 3",
    "gh -R a/b -R c/d pr merge 3",
    "bash -c 'gh pr merge 1'",
    'sh -c "gh pr merge 1"',
    "bash -lc 'cd x && gh pr merge 1'",
    "bash -e -c 'gh pr merge 1'",
    "/bin/bash -c 'bash -c \"gh pr merge 1\"'",
    "eval 'gh pr merge 1'",
    "eval gh pr merge 1",
    "xargs gh pr merge",
    "if true; then gh pr merge 1; fi",
    "for i in 1; do gh pr merge $i; done",
    "while false; do :; done; gh pr merge 1",
    "! gh pr merge 1",
    "echo hi\ngh pr create",
    "echo hi \\\n && gh pr create",
    "cat <<EOF\nbody\nEOF\ngh pr merge 1",
    "cat <<-'EOF'\n\tbody\n\tEOF\ngh pr merge 1",
    "cat <<EOF >out && gh pr merge 1\nbody\nEOF",
    "echo 'a' # c\ngh pr merge 1",
    "echo \\' ; gh pr merge 1",
    'echo "\\"" ; gh pr merge 1',
    "echo hi # gh pr view\ngh pr merge 1",
]


ALLOWED = [
    "",
    "ls",
    "gh pr view 1",
    "gh pr list",
    "gh pr checks 1",
    "gh pr diff 1",
    "gh pr status",
    "gh pr",
    "gh pr --help",
    "gh -R a/b",
    "gh -R",
    "gh api repos/a/b/pulls/1/merge -X PUT",
    "gh issue create",
    "mise run pr -- create",
    "mise run pr -- edit --pr 3",
    "mise run merge -- 3 --dry-run",
    "uv run tbhbdev alpha pr create",
    "uv run tbhbdev alpha merge 3",
    "git commit -m 'docs: mention gh pr merge'",
    'git commit -m "docs: mention gh pr merge"',
    "git commit -m 'a' -m 'gh pr create'",
    "echo 'gh pr merge 1'",
    'echo "gh pr merge 1"',
    "grep -r 'gh pr create' docs",
    "echo gh pr merge # not run",
    "cat <<EOF\ngh pr merge 1\nEOF",
    "cat <<-EOF\n\tgh pr merge 1\n\tEOF",
    "git commit -m \"$(cat <<'EOF'\ndocs: gh pr merge note\n\ngh pr create\nEOF\n)\"",
    "cat <<EOF",
    "cat << EOF\ngh pr merge 1\nEOF",
    "bash script.sh gh pr merge",
    "bash -c",
    "bash -e",
    "bash --norc -c 'echo ok'",
    "eval",
    "env",
    "env -i",
    "echo $((1 + 2))",
    "gh pr view 1 && gh pr checks 1 | cat",
    "echo 'unterminated gh pr merge",
    "echo \\",
]


def run_find(text: str) -> guard.Match | None:
    """Scan one command."""
    return guard.find(text)


@pytest.mark.parametrize("text", BLOCKED)
def test_blocked(text: str) -> None:
    assert run_find(text) is not None


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed(text: str) -> None:
    assert run_find(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gh pr create --title x", CREATE),
        ("gh pr edit 12", EDIT.format(n=12)),
        ("gh pr edit --title x", EDIT.format(n="<N>")),
        ("gh -R a/b pr edit '#34' --title x", EDIT.format(n=34)),
        ("gh pr edit https://github.com/a/b/pull/56", EDIT.format(n=56)),
        ("gh pr merge 7 --squash", MERGE.format(n=7)),
        ("gh pr merge --auto 8", MERGE.format(n=8)),
        ("gh pr merge", MERGE.format(n="<N>")),
        ("gh pr merge feature-branch", MERGE.format(n="<N>")),
        ("gh pr close 5 --repo a/b -c done", PR_CLOSE.format(n=5)),
        ("gh pr close", PR_CLOSE.format(n="<N>")),
        ("gh issue close 6 --reason 'not planned'", ISSUE_CLOSE.format(n=6)),
        ("gh -R a/b issue close 6", ISSUE_CLOSE.format(n=6)),
        ("gh run rerun 123 --failed", RERUN),
        (
            "gh api -X DELETE repos/a/b/git/refs/heads/feat/x",
            DELETE_BRANCH.format(n="<N>"),
        ),
        (
            "gh api repos/{owner}/{repo}/git/refs/heads/x --method delete",
            DELETE_BRANCH.format(n="<N>"),
        ),
        (
            "gh api graphql -f 'query=mutation { resolveReviewThread(input: $input) }'",
            THREAD_HINT.format(command="resolve"),
        ),
        ("gh pr ready 399", READY.format(n=399)),
        ("gh pr ready", READY.format(n="<N>")),
        (
            "gh pr ready 399 --undo",
            (
                "`gh pr ready` is blocked. `tbhbdev alpha pr ready` has no undo. "
                "Ask the operator to return the PR to draft."
            ),
        ),
    ],
)
def test_hint(text: str, expected: str) -> None:
    found = run_find(text)
    assert found is not None
    assert guard.hint(found) == expected


def test_branch_delete_match_preserves_api_arguments() -> None:
    args = ["-X", "DELETE", "repos/a/b/git/refs/heads/x"]
    assert guard.gh_match(["-R", "a/b", "api", *args]) == ("delete-branch", args, "api")


@pytest.mark.parametrize(
    "text",
    [
        'echo "gh pr close 1"',
        "echo 'gh issue close 1; gh run rerun 2'",
        "gh issue list",
        "gh run view 1",
        "gh api repos/a/b/git/refs/heads/x",
        "gh api -X GET repos/a/b/git/refs/heads/x",
        "gh api -X DELETE repos/a/b/issues/1",
        "gh api -X DELETE",
        "gh api",
        "gh api graphql -f 'query=query { viewer { login } }'",
        "gh api graphql -f 'query={ search(query: \"resolveReviewThread(a)\") }'",
        "gh api graphql -f 'input=resolveReviewThread(...)'",
        "gh api repos/a/b/issues -f 'query=mutation { resolveReviewThread(...) }'",
    ],
)
def test_new_hints_leave_other_commands_alone(text: str) -> None:
    assert run_find(text) is None


@pytest.mark.parametrize(
    ("command", "named"),
    [
        ("gh pr create", "tbhbdev alpha pr create"),
        ("gh pr edit 3", "tbhbdev alpha pr edit .github/PULL_REQUEST_DRAFT.md --pr 3"),
        ("gh pr merge 3", "tbhbdev alpha merge 3"),
        ("gh pr ready 3", "tbhbdev alpha pr ready 3"),
        ("gh pr close 3", "tbhbdev alpha pr close 3 --reason TEXT"),
        ("gh issue close 3", "tbhbdev alpha issue close 3 --reason"),
        ("gh run rerun 9", "tbhbdev alpha pr rerun N"),
        (
            "gh api -X DELETE repos/a/b/git/refs/heads/x",
            "tbhbdev alpha pr delete-branch",
        ),
        (
            "gh api graphql --field='query=mutation { unresolveReviewThread(a: $i) }'",
            "tbhbdev alpha review unresolve <PR> <THREAD>",
        ),
    ],
)
def test_every_denial_names_the_command_and_the_alpha_footer(
    command: str,
    named: str,
) -> None:
    message = guard.repository_message(command, in_repository=True)
    assert message is not None
    first, *_, last = message.split("\n")
    assert first == "tbhbdev hint:"
    assert named in message
    assert last == (
        "Use `tbhbdev alpha` for GitHub writes: `tbhbdev alpha --help` lists the "
        "commands, and `tbhbdev alpha session-context` reports this session's context."
    )


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'gh pr close 5'",
        "git commit -m 'gh issue close 5'",
        "git commit -m 'gh run rerun 5'",
        "git commit -m 'gh api -X DELETE repos/a/b/git/refs/heads/x'",
    ],
)
def test_a_lookalike_in_a_commit_message_passes(
    command: str,
) -> None:
    assert guard.repository_message(command, in_repository=True) is None


def test_first_match_wins() -> None:
    found = run_find("gh pr merge 1; gh pr create")
    assert found is not None
    assert found.sub == "merge"


def test_scanner_backtick_nested_in_dollar_paren_restores_quotes() -> None:
    # The quote opened before `$(` is restored at `)`, so the text after it is
    # still quoted and is not a command.
    assert run_find('echo "$(true) ; gh pr merge 1"') is None


def test_unbalanced_close_paren_is_harmless() -> None:
    assert run_find(") gh pr merge 1") is not None


def test_heredoc_without_closing_line_swallows_the_rest() -> None:
    assert run_find("cat <<EOF\ngh pr merge 1") is None


def test_here_string_is_not_a_heredoc() -> None:
    assert run_find("cat <<< word\ngh pr merge 1") is not None


def test_two_heredocs_on_one_line() -> None:
    text = "cat <<A <<B\nx\nA\ny\nB\ngh pr merge 1"
    assert run_find(text) is not None
    assert run_find("cat <<A <<B\nx\nA\ngh pr merge 1\nB\n") is None


WORD = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Nd"), whitelist_characters="-_./"
    ),
    min_size=1,
    max_size=8,
).filter(lambda w: not w.startswith(("-", "#")) and "=" not in w)


SEP = st.sampled_from(["; ", " && ", " || ", " | ", " & ", "\n", " ; "])


SUB = st.sampled_from(guard.SUBCOMMANDS)


LEAD = st.sampled_from(
    ["", "FOO=1 ", "env ", "env -i A=b ", "sudo ", "time ", "command ", "! ", "{ "]
)


GH = st.sampled_from(["gh", "/usr/bin/gh", "gh -R a/b", "gh --repo a/b", "gh -q"])


@given(before=st.lists(WORD, max_size=3), sep=SEP, lead=LEAD, gh=GH, sub=SUB)
def test_property_gh_pr_after_any_boundary_is_blocked(
    before: list[str], sep: str, lead: str, gh: str, sub: str
) -> None:
    prefix = " ".join(["echo", *before]) + sep
    found = run_find(f"{prefix}{lead}{gh} pr {sub} 5")
    assert found is not None
    assert found.sub == sub


@given(sub=SUB, wrapper=st.sampled_from(["bash -c", "sh -c", "zsh -lc", "eval"]))
@pytest.mark.parametrize("quote", ["'", '"'])
def test_property_shell_wrappers_are_scanned(
    sub: str, wrapper: str, quote: str
) -> None:
    found = run_find(f"{wrapper} {quote}cd x; gh pr {sub} 5{quote}")
    assert found is not None
    assert found.sub == sub


@given(words=st.lists(WORD, max_size=4), sub=SUB, quote=st.sampled_from(["'", '"']))
def test_property_quoted_text_of_a_plain_command_is_not_run(
    words: list[str], sub: str, quote: str
) -> None:
    arg = f"{quote}{' '.join(words)} gh pr {sub} 5{quote}"
    assert run_find(f"git commit -m {arg}") is None
    assert run_find(f"echo {arg}") is None


@given(
    verb=st.sampled_from(["view", "list", "checks", "diff", "status"]),
    gh=GH,
)
def test_property_other_pr_subcommands_are_allowed(verb: str, gh: str) -> None:
    assert run_find(f"{gh} pr {verb} 5") is None


@given(st.text(max_size=80))
def test_property_scanner_never_raises(text: str) -> None:
    run_find(text)


@pytest.mark.parametrize(
    "text",
    [
        "gh pr review 7 -R tbhb-dev/tbhbdev",
        "gh pr review 7 -R tbhb-dev/tbhbdev --comment -b ok",
        "gh pr review 7 -R tbhb-dev/tbhbdev -b -a",
        "gh pr review 7 -R tbhb-dev/tbhbdev -ba",
        "gh pr review 7 -R tbhb-dev/tbhbdev --request-changes -b no",
        "gh pr view 7 -R tbhb-dev/tbhbdev",
        "gh api repos/tbhb-dev/tbhbdev/pulls/7/reviews",
        "gh api -X GET repos/tbhb-dev/tbhbdev/pulls/7/reviews -f event=APPROVE",
        "gh api --method=GET repos/tbhb-dev/tbhbdev/pulls/7/reviews -F event=APPROVE",
        "gh api -X POST repos/tbhb-dev/tbhbdev/pulls/7/reviews -f event=COMMENT",
        "gh api repos/tbhb-dev/tbhbdev/pulls/7/reviews -f body=APPROVE",
        "gh api repos/tbhb-dev/tbhbdev/pulls/7/reviews -f event",
        "gh api -X POST repos/tbhb-dev/tbhbdev/pulls/7/reviews --input f.json",
        "gh api repos/tbhb-dev/tbhbdev/pulls/7/comments -f event=APPROVE",
        "gh api repos/tbhb-dev/tbhbdev/pulls/7/merge -X PUT -f event=APPROVE",
        "gh api repos/tbhb-dev/tbhbdev/issues/7/reviews -f event=APPROVE",
        "gh api graphql -f query=addPullRequestReview",
        "gh -R tbhb-dev/tbhbdev api -f event=APPROVE",
        "git commit -m 'gh pr review 7 --approve -R tbhb-dev/tbhbdev'",
        "gh issue view 7",
        "gh pr",
    ],
)
def test_other_review_calls_pass(text: str) -> None:
    assert guard.approval_message(text) is None


@pytest.mark.parametrize(
    ("word", "approve"),
    [
        ("--approve", True),
        ("--approve=false", True),
        ("--approved", False),
        ("-a", True),
        ("-ca", True),
        ("-ba", False),
        ("-Fa", False),
        ("-Ra", False),
        ("-c", False),
        ("--a", False),
        ("a", False),
    ],
)
def test_approves(word: str, *, approve: bool) -> None:
    assert guard.approves(word) is approve


def test_only_gh_itself_is_matched() -> None:
    assert guard.find("echo pr create x") is None
    assert guard.find("/usr/bin/gh pr create x") == guard.Match("create", ["x"])


def test_a_value_word_that_looks_like_a_field_is_not_one() -> None:
    text = "gh api repos/tbhb-dev/tbhbdev/pulls/7/reviews -H -f event=APPROVE -X POST"
    assert guard.approval_message(text) is None


@pytest.mark.parametrize(
    ("text", "verb"),
    [
        ("gh -R example/repo pr review 7 -a", "`gh pr review --approve`"),
        ("gh pr review 7 -aR example/repo", "`gh pr review --approve`"),
        (
            "gh api repos/{owner}/{repo}/pulls/7/reviews -f event=APPROVE",
            "a `gh api` APPROVE review",
        ),
        (
            "gh api repos/o/r/pulls/7/reviews -f event=APPROVE -H --method=GET",
            "a `gh api` APPROVE review",
        ),
        (
            "gh api repos/o/r/pulls/7/reviews -f body -f event=APPROVE",
            "a `gh api` APPROVE review",
        ),
    ],
)
def test_first_approval_route_wins(text: str, verb: str) -> None:
    assert guard.approval_message(text + "; tbhb-approve tools 7") == (
        f"{guard.GH_RULE} The hook blocks {verb}."
    )
