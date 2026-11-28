# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise GitHub polling and token decisions on shell text."""

import itertools
import shlex

import pytest

from tbhbdev.alpha.guard.gh_poll import decision as guard
from tbhbdev.alpha.guard.gh_poll.token import UNSET_REASON

SHA = "abcdef0123456789abcdef0123456789abcdef01"


TOKEN = "OP_SERVICE_" + "ACCOUNT_TOKEN"


FIELDS = (
    "databaseId,event,conclusion,createdAt,startedAt,updatedAt,"
    "workflowName,attempt,headBranch"
)


def listing(repo: str) -> str:
    return f"gh run list -R {repo} --limit 100 --json {FIELDS}"


@pytest.mark.parametrize(
    "command",
    [
        # watch and list commands
        "gh run watch 42",
        "gh run ls",
        "gh run ls -R org/repo --limit 100",
        "gh run list --limit 2",
        "gh run list --limit 10",
        "gh run list -L 1x",
        "gh run list --limit",
        "gh api 'repos/o/r/actions/runs?per_page=2'",
        "gh api 'repos/o/r/actions/runs?per_page=1' --paginate",
        "gh api 'repos/o/r/actions/runs?per_page=1&per_page=1'",
        "for r in a b; do gh run list -R $r --limit 1; done",
        "gh run -R org/repo list",
        "gh run --repo=org/repo ls",
        "gh run -R org/repo watch 1",
        "$'gh' run list",
        "gh api --method='' repos/o/r/actions/runs",
        "gh api -X POST --method='' repos/o/r/actions/runs",
        "gh api --method='' -X POST --method='' repos/o/r/actions/runs",
        "gh run list",
        "gh pr checks --watch",
        "gh pr checks 3 --watch=true",
        "gh -R org/repo run list",
        "gh --repo=org/repo run list",
        "gh --hostname=example.com -R org/repo run list",
        "gh -R org/repo pr checks 1 --watch",
        "/opt/bin/gh --hostname example.com run watch",
        # pagination, on any endpoint and with any method
        "gh query --paginate rate_limit",
        "gh api rate_limit --paginate=true",
        "gh api --paginate repos/o/r/actions/runs/1",
        "gh api --method POST --paginate repos/o/r/actions/runs",
        "gh api --input body.json --paginate x",
        "gh api graphql --paginate -f query=x",
        # list endpoints
        "gh api repos/o/r/actions/runs",
        "gh api repos/o/r/actions/runs/",
        "gh api repos/{owner}/{repo}/actions/runs",
        "gh api /repos/o/r/actions/jobs?per_page=2",
        "gh query repos/o/r/check-runs",
        "gh api https://api.github.com/repos/o/r/check-suites/",
        "gh api https://ghe.example/api/v3/repos/o/r/actions/runs",
        "gh api repos/o/r/actions/workflows/build.yml/runs",
        f"gh api repos/o/r/actions/workflows/build.yml/runs?head_sha={SHA}",
        "gh api repos/o/r/actions/runs?head_sha=abc",
        f"gh api repos/o/r/actions/runs?head_sha={SHA[:-1]}",
        f"gh api repos/o/r/actions/runs?head_sha={SHA} --paginate",
        f"gh api 'repos/o/r/actions/runs?head_sha={SHA}&head_sha={SHA}'",
        "gh api rate_limit repos/o/r/actions/runs",
        "gh api -X get repos/o/r/actions/runs",
        "gh api --method=HEAD repos/o/r/check-runs",
        "gh api -F status=queued -X GET repos/o/r/actions/runs",
        # prefixes
        "A=1 gh run list",
        "sudo -E gh run list",
        "env -C /work gh run list",
        "env --chdir=/work -S 'gh run list'",
        "env -u OTHER gh run list",
        "env --unset OTHER gh run list",
        # loops
        "while true; do gh api rate_limit; done",
        "until gh pr view 1; do echo waiting; done",
        "for id in 1 2; do gh api repos/o/r/pulls/$id; done",
        "while pgrep -f fetch.py; do sleep 5; done",
        "while pgrep -f fetch.py; do sleep 5",
        "until sleep 1; do /usr/bin/pgrep -f fetch; done",
        "for x in a; do for y in b; do echo x; done; gh pr view; done",
        "{ while true; do gh pr view; done; }",
        "if true; then while true; do gh pr view; done; fi",
        "while true\ndo\n  gh pr view 1\n  sleep 30\ndone",
        "while true; do sleep 5; pgrep -fl fetch; done",
        "while true; do sleep 5; pgrep --full fetch; done",
        "for x in a; do eval 'gh pr view'; done",
        "while true; do ssh host 'gh pr view'; done",
        "while true; do bash -c 'gh api rate_limit'; done",
        "while true; do sh <<EOF\ngh pr view\nEOF\ndone",
        # nested scripts
        "bash -c 'while true; do gh pr view; done'",
        "zsh -lc 'gh run list'",
        "bash --noprofile -lc 'gh run list'",
        "eval 'gh run list'",
        "eval gh run list",
        "echo $(gh run list)",
        'echo "`gh run list`"',
        "ssh -p 22 host 'gh run list'",
        "ssh -t host gh run list",
        "ssh -o BatchMode=yes host 'while pgrep -f x; do sleep 1; done'",
        # heredoc bodies a shell or ssh runs
        "bash <<EOF\ngh run list\nEOF",
        "bash -s <<'EOF'\nwhile pgrep -f fetch.py; do sleep 5; done\nEOF",
        "sh -x <<-EOF\n\tgh run list\n\tEOF",
        "ssh host <<EOF\ngh run list\nEOF",
        "cat <<A; bash <<B\nhello\nA\ngh run list\nB",
        "cat <<EOF\ngh api rate_limit\nEOF\ngh run list",
    ],
)
def test_denies_polling(command: str) -> None:
    assert guard.inspect(command)[0] == guard.POLL_REASON


@pytest.mark.parametrize(
    "command",
    [
        "",
        "true",
        "gh",
        "gh api",
        "gh run",
        "gh run view 42",
        "gh run view 42 --log-failed",
        "gh pr checks",
        "gh pr checks 3",
        "gh pr checks --watchful",
        "gh pr view 1",
        "gh api repos/o/r/actions/runs/42",
        "gh api repos/o/r/actions/runs/42/",
        "gh api repos/o/r/actions/runs/42/jobs",
        "gh api repos/o/r/actions/jobs/123",
        "gh api repos/o/r/check-runs/1",
        "gh api repos/o/r/check-suites/1",
        "gh api repos/o/r/check-suites/X",
        "gh api repos/o/r/pulls/1",
        f"gh api repos/o/r/commits/{SHA}/check-runs",
        f"gh api repos/o/r/actions/runs?head_sha={SHA}",
        f"gh api 'repos/o/r/actions/runs?head_sha={SHA}&per_page=5'",
        "gh api 'repos/o/r/actions/runs?per_page=1'",
        "gh api 'repos/o/r/actions/workflows/w.yml/runs?per_page=1'",
        "gh run list --limit 1",
        "gh run list -L 1",
        "gh run list --limit=1 -R o/r",
        "gh api rate_limit",
        "gh query rate_limit",
        "gh api -X POST repos/o/r/actions/runs",
        "gh api --method=PATCH repos/o/r/check-runs",
        "gh api -f name=x repos/o/r/actions/runs",
        "gh api --input body.json repos/o/r/check-runs",
        "gh pr list",
        "gh run view 5",
        "gh run -R org/repo view 5",
        "gh api --method='' -f name=x repos/o/r/actions/runs",
        # quoted text and heredoc bodies are data
        "echo 'gh run list'",
        "git commit -m 'while gh run list'",
        "cat <<'EOF'\nwhile pgrep -f fetch.py; do sleep 5; done\nEOF",
        "cat <<EOF\ngh run list\nEOF",
        "bash script.sh <<EOF\ngh run list\nEOF",
        "bash -c 'echo hi' <<EOF\ngh run list\nEOF",
        "ssh host 'echo hi' <<EOF\ngh run list\nEOF",
        # loops that neither run gh nor wait on pgrep
        "for x in a; do echo gh sleep pgrep; done",
        "while true; do sleep 5; done",
        "while pgrep fetch; do echo waiting; done",
        # a local wait by process name, or with a -f pattern that can't match itself
        "while pgrep fetch; do sleep 5; done",
        "until sleep 1; do /usr/bin/pgrep fetch; done",
        "while true; do sleep 5; pgrep fetch; done",
        "while pgrep -f '[f]etch.py'; do sleep 5; done",
        "ssh -o BatchMode=yes host 'while pgrep x; do sleep 1; done'",
        "for x in a; do true; done; gh pr view",
        "while true; do true; done; pgrep fetch; sleep 5",
        "while true; do for x in a; do true; done; sleep 1; done; pgrep x",
        "echo while; gh pr view",
        "echo for; sleep 1; pgrep x",
        # shells, eval and ssh without a polling script
        "bash script.sh",
        "bash --noprofile -lc 'echo gh'",
        "eval 'echo gh'",
        "ssh host",
        "ssh -p 22",
        "ssh -p 22 host",
        "env -u OTHER gh api rate_limit",
        "env --unset=OTHER true",
        "command -- /bin/true",
        "env -u OTHER env -- true",
        "env -u",
        "do",
        "done",
    ],
)
def test_allows_object_reads_and_data(command: str) -> None:
    assert guard.inspect(command)[0] is None


UNSETS = [
    f"unset {TOKEN}",
    f"unset -v OTHER {TOKEN}",
    f"env -u {TOKEN} op whoami",
    f"env --unset {TOKEN} op whoami",
    f"env --unset={TOKEN} op whoami",
    f"env -u{TOKEN} op whoami",
]


WRAPS = ["{}", "bash -c '{}'", "zsh -lc '{}'", "eval '{}'", "ssh host '{}'"]


@pytest.mark.parametrize(
    "command",
    [
        *(wrap.format(unset) for wrap, unset in itertools.product(WRAPS, UNSETS)),
        f"builtin unset -v {TOKEN}",
        f"A=x command env --unset={TOKEN} op whoami",
        f"/usr/bin/env -u {TOKEN} true",
        f"env -i -u {TOKEN} true",
        f"env -u OTHER -u {TOKEN} true",
        f"sudo env -u {TOKEN} true",
        f"while true; do unset {TOKEN}; done",
        f"bash <<EOF\nunset {TOKEN}\nEOF",
        f"echo $(unset {TOKEN})",
    ],
)
def test_denies_token_unsets(command: str) -> None:
    assert guard.inspect(command)[0] == UNSET_REASON


@pytest.mark.parametrize(
    "command",
    [
        "unset OTHER",
        "unset",
        f"echo 'unset {TOKEN}'",
        f'git commit -m "docs: never unset {TOKEN}"',
        f"printf '%s' 'env --unset={TOKEN}'",
        f"env --unset=OTHER echo {TOKEN}",
        f"echo {TOKEN}",
        "env -u OTHER true",
        "env --unset",
        f"ssh -o BatchMode=yes host 'echo {TOKEN}'",
        f"cat <<EOF\nunset {TOKEN}\nEOF",
        f"cat <<'EOF'\nenv -u {TOKEN} op whoami\nEOF",
        f"op run --env-file x -- printenv {TOKEN}",
    ],
)
def test_allows_other_unsets_and_quoted_mentions(command: str) -> None:
    assert guard.inspect(command)[0] is None


def wrapped(script: str) -> list[str]:
    """The script alone, and run by sh -c, bash -c, eval and ssh."""
    quoted = shlex.quote(script)
    return [
        script,
        f"sh -c {quoted}",
        f"bash -c {quoted}",
        f"eval {quoted}",
        f"ssh host {quoted}",
    ]


MORE_UNSETS = [
    # env: option clusters, option arguments and -S
    f"env -iu{TOKEN} op whoami",
    f"env -i -u {TOKEN} op whoami",
    f"env -C /work -u {TOKEN} op whoami",
    f"env -C/work -u {TOKEN} op whoami",
    f"env -vC /work -u{TOKEN} op whoami",
    f"env --chdir=/work -u {TOKEN} op whoami",
    f"env --chdir /work --unset {TOKEN} op whoami",
    f"env --argv0 x --unset={TOKEN} op whoami",
    f"env --uns {TOKEN} op whoami",
    f"env --un={TOKEN} op whoami",
    f"env -S '-u {TOKEN} op whoami'",
    f"env -S'-u {TOKEN}' op whoami",
    f"env --split-string='-C /work -u {TOKEN}' op whoami",
    f"env --split-string 'A=1 env -u {TOKEN} op whoami'",
    f'env -S "-u {TOKEN} \'op whoami"',
    f"env -u OTHER -S '-u {TOKEN}' op whoami",
    f"env -u ${TOKEN} op whoami",
    f"env -u '${{{TOKEN}}}' op whoami",
    # env drops the whole environment
    "env -i op whoami",
    "env -i",
    "env -0i op whoami",
    "env -vi op whoami",
    "env --ignore-environment op whoami",
    "env --ignore-e op whoami",
    "env --ignore-environment",
    "env - op whoami",
    "env -u OTHER -i op whoami",
    "env -C /work -i op whoami",
    "env -i gh pr view 1",
    "sudo env -i op whoami",
    "A=1 command env -i true",
    "env -S '-i op whoami'",
    # un-export
    f"export -n {TOKEN}",
    f"export -n {TOKEN}=x",
    f"export -fn {TOKEN}",
    f"export -nf {TOKEN}",
    f"export -n OTHER {TOKEN}",
    f"export -n -- {TOKEN}",
    f"export -n ${TOKEN}",
    f"declare +x {TOKEN}",
    f"declare -g +x {TOKEN}",
    f"declare +ix {TOKEN}",
    f"typeset +x {TOKEN}",
    f"local +x {TOKEN}",
    f"builtin export -n {TOKEN}",
    # an assignment that empties it
    f"{TOKEN}= op whoami",
    f"{TOKEN}='' op whoami",
    f'{TOKEN}="" op whoami',
    f"{TOKEN}=",
    f"{TOKEN}=''",
    f'{TOKEN}=""',
    f"A=1 {TOKEN}= op whoami",
    f"env {TOKEN}= op whoami",
    f"env -u OTHER {TOKEN}='' op whoami",
    f"sudo -E {TOKEN}= op whoami",
    f"export {TOKEN}=",
    f"export {TOKEN}=''",
    f'export A=1 {TOKEN}=""',
    f"declare -x {TOKEN}=",
    f"declare {TOKEN}=",
    f"typeset -x {TOKEN}=",
    f"local {TOKEN}=",
    f"export -- {TOKEN}=",
    # the unset builtin's other spellings of the name
    f"unset ${TOKEN}",
    f"unset ${{{TOKEN}}}",
    f'unset "${TOKEN}"',
    f'unset "${{{TOKEN}}}"',
    f"unset -v A ${{{TOKEN}}}",
]


@pytest.mark.parametrize(
    "command",
    [wrap for unset in MORE_UNSETS for wrap in wrapped(unset)],
)
def test_denies_more_token_unsets(command: str) -> None:
    assert guard.inspect(command)[0] == UNSET_REASON


@pytest.mark.parametrize(
    "command",
    [
        # env options that aren't unsets, and option arguments
        "env true",
        "env -v true",
        "env -0 true",
        "env --debug true",
        "env --null true",
        "env -C /work true",
        "env --chdir=/work true",
        "env -u OTHER -C /work true",
        "env -S 'A=1 true'",
        "env -S 'A=1 \"x' true",
        "env -S",
        f"env -C {TOKEN} true",
        f"env --chdir {TOKEN} true",
        f"env --argv0 {TOKEN} true",
        f"env -a {TOKEN} true",
        f"env -S 'echo {TOKEN}'",
        f"env -C -u {TOKEN} true",
        # not env's options once the command starts, or env rejects them
        f"env true -u {TOKEN}",
        "env true -i",
        "env -- -i true",
        "env --ig true",
        "env --i true",
        "env --bogus true",
        "env --=x true",
        "env -ux true",
        # export, declare, typeset and local that keep it
        "export -n OTHER",
        "export -n",
        "export OTHER=",
        "export -p",
        f"export {TOKEN}",
        f"export {TOKEN}=value",
        f"export A=1 -n {TOKEN}",
        f"export -f {TOKEN}",
        "declare +x OTHER",
        f"declare -x {TOKEN}",
        f"typeset -x {TOKEN}",
        f"local {TOKEN}",
        f"local -x {TOKEN}=value",
        f"declare +i {TOKEN}",
        f"{TOKEN}=value op whoami",
        f"{TOKEN}=$A op whoami",
        f"OTHER= {TOKEN}=1 true",
        "OTHER= true",
        "OTHER=''",
        # data and out-of-scope forms
        f"echo {TOKEN}=",
        f"echo 'export -n {TOKEN}'",
        f"git commit -m 'env -i and {TOKEN}= and export -n {TOKEN}'",
        f"printf '%s' '{TOKEN}='",
        f"cat <<EOF\nenv -i op whoami\nexport -n {TOKEN}\nEOF",
        f"echo ${TOKEN} ${{{TOKEN}}}",
        "unset $OTHER",
        f'n={TOKEN}; unset "$n"',
        f"unset {TOKEN}x",
        f"unset x{TOKEN}",
        f"unset ${TOKEN}x",
    ],
)
def test_allows_option_arguments_and_other_variables(command: str) -> None:
    assert guard.inspect(command)[0] is None


def test_the_first_denial_wins() -> None:
    assert guard.inspect(f"unset {TOKEN}; gh run list")[0] == UNSET_REASON
    assert guard.inspect(f"gh run list; unset {TOKEN}")[0] == guard.POLL_REASON


def test_inspect_reports_the_programs_it_runs() -> None:
    assert guard.inspect("echo a | bash -c 'gh pr view; sleep 1'") == (
        None,
        {"echo", "bash", "gh", "sleep"},
    )
    assert guard.inspect("A=1 env -u OTHER") == (None, set())
    assert guard.inspect("echo hi; gh run list") == (guard.POLL_REASON, {"echo"})


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["while", "true"], True),
        (["{", "until", "x"], True),
        (["do", "for", "x"], True),
        (["echo", "while"], False),
        (["if", "!"], False),
        ([], False),
    ],
)
def test_opens_loop(words: list[str], *, expected: bool) -> None:
    assert guard.opens_loop(words) is expected


def test_reasons_tell_the_agent_to_subscribe_through_awrel() -> None:
    assert guard.POLL_REASON == (
        "Don't poll GitHub. Subscribe to the awrel event instead, for example "
        "check_run conclusion events for the repository through awrel, and act "
        "when it arrives. Bulk and polling GitHub reads are blocked."
    )
    unset = f"Don't unset {TOKEN}. Leave the environment as the harness gives it."
    assert unset == UNSET_REASON


def test_checked_accepts_an_empty_command() -> None:
    assert guard.checked([], None) == (None, set())


@pytest.mark.parametrize(
    "command",
    [
        f"printf 'a\\nb\\n' | xargs -n1 sh -c '{listing('example/one')}'",
        f"xargs -n 1 {listing('example/one')}",
        f"parallel {listing('example/one')}",
        f"seq 2 | /usr/bin/xargs -I{{}} {listing('example/one')}",
    ],
)
def test_xargs_and_parallel_cannot_repeat_a_listing(command: str) -> None:
    assert guard.inspect(command)[0] == guard.REPEAT_REASON


QUOTED_LIST = "'gh r\"\"un list -R example/three --json databaseId'"


@pytest.mark.parametrize(
    "command",
    [
        "printf 'list -R example/one --limit 100 --json "
        + FIELDS
        + "\\n' | xargs -L1 gh run",
        "xargs -L1 -a args gh run",
        "parallel gh run :::: args",
    ],
)
def test_a_repeater_cannot_take_the_listing_words_from_input(command: str) -> None:
    assert guard.inspect(command)[0] == guard.REPEAT_REASON


def test_xargs_may_run_other_gh_commands() -> None:
    assert guard.inspect("echo 1 2 | xargs -n1 gh pr view")[0] is None


def test_parallel_may_run_other_commands() -> None:
    assert guard.inspect("parallel -j2 echo ::: a b")[0] is None


def test_xargs_cannot_repeat_a_listing_split_by_a_line_continuation() -> None:
    script = listing("example/one").replace("run list", "run \\\nlist")
    command = f"printf 'a\\nb\\n' | xargs -n1 sh -c '{script}'"
    assert guard.inspect(command)[0] == guard.REPEAT_REASON


def test_repeated_scan_collects_the_programs_of_every_suffix() -> None:
    assert guard.repeated_scan(["echo", "ls"]) == (None, {"echo", "ls"})


@pytest.mark.parametrize(
    "command",
    [
        "printf 'list\\nlist\\n' | env -S 'xargs -L1 gh run'",
        "env -S 'parallel gh run'",
        "printf 'a\\nb\\n' | xargs -n 1 sh -c " + QUOTED_LIST,
        "printf 'a\\nb\\n' | xargs -L 1 sh -c 'gh r\"\"un list -R example/three'",
    ],
)
def test_a_repeater_hidden_in_an_option_value_or_env_string_is_denied(
    command: str,
) -> None:
    assert guard.inspect(command)[0] is not None


def test_flattened_splits_quoted_words_to_a_depth() -> None:
    assert guard.flattened(["env", "-S", "xargs gh r''un"]) == [
        "env", "-S", "xargs", "gh", "run",
    ]  # fmt: skip
    assert guard.flattened(["a b"], depth=0) == ["a b"]
    assert guard.flattened(["'a b'"], depth=0) == ["'a b'"]
    assert guard.flattened(['x."count"']) == ["x.count"]
    assert guard.flattened(["plain"]) == ["plain"]


@pytest.mark.parametrize(
    "command",
    [
        "xargs gh api repos/o/r/actions/runs",
        "parallel gh api repos/o/r/actions/runs",
        "parallel gh pr checks 1 --watch",
        "xargs gh pr checks 1 --watch",
        "xargs env -u " + TOKEN + " true",
    ],
)
def test_a_repeater_still_scans_what_it_runs(command: str) -> None:
    assert guard.inspect(command)[0] is not None


def test_flattened_splits_nested_words_four_levels_deep() -> None:
    levels = [shlex.join(["a", "t1"])]
    for n in range(2, 7):
        levels.append(shlex.join([levels[-1], f"t{n}"]))
    # Splitting the sixth level peels it and the three below it; the second stays whole.
    assert guard.flattened([levels[5]]) == [levels[1], "t3", "t4", "t5", "t6"]


def test_checked_passes_the_repeated_flag_to_the_listing_check() -> None:
    command = ["gh", "run", "list", "--limit", "1"]
    assert guard.checked(command, None, repeated=True)[0] == guard.POLL_REASON
    assert guard.checked(command, None)[0] is None


@pytest.mark.parametrize(
    "command",
    [
        "seq 2 | xargs -I{} gh api 'repos/o/r/actions/runs?per_page=1'",
        "gh run list --limit 1 --limit 100",
        "gh run list -L 1 -L 100",
        "gh run list -L1 -L100",
    ],
)
def test_a_single_read_allowance_does_not_cover_repeats_or_a_later_limit(
    command: str,
) -> None:
    assert guard.inspect(command)[0] is not None


def test_an_attached_short_limit_of_one_is_a_single_read() -> None:
    assert guard.inspect("gh run list -L1")[0] is None


def test_measurement_listing_is_denied_without_an_exception() -> None:
    command = "gh run list -R example/one --limit 100 --json databaseId"
    assert guard.inspect(command)[0] == guard.POLL_REASON


def test_listing_after_a_redirect_is_denied() -> None:
    assert guard.inspect(">out gh run list")[0] == guard.POLL_REASON
