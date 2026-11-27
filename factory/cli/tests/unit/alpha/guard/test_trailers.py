# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise commit trailer decisions with shell commands only."""

import pytest

from tbhbdev.alpha.guard import trailers as state_guard


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "x\n\nSigned-off-by: A <a@example.com>"',
        "git commit --signoff -m x",
        "git commit --trailer 'Co-authored-by: A <a@example.com>' -m x",
        "git add . && git commit --trailer=signed-off-by:x -m x",
        "git commit -s -m x",
        "git commit -m x -s",
        "git commit -as -m x",
        "git commit -sm x",
        "env X=1 git -C d commit -s",
        "git --no-pager commit -s",
        "command git commit -s",
        "exec git commit -s",
        "X=1 Y=2 git commit -s",
        "git -c user.name=x -C d commit -s",
        "/usr/bin/git commit -s",
        "true && git commit --signoff",
        "true || git commit -s",
        "true; git commit -s",
        "true | git commit -s",
        "true & git commit -s",
        "echo hi\ngit commit -s",
        "$(git commit -s)",
        "x=$(git commit -s)",
        "(git commit -s)",
        "{ git commit -s; }",
        "echo `git commit -s`",
        "echo 'a;b' && git commit -s",
        'echo "a;b" && git commit -s',
        "echo \\' && git commit -s",
        "cat <<EOF\nhi\nEOF\ngit commit -s",
        'grep a <<< "x"\ngit commit -s -m y',
        'grep a <<<< "x"\ngit commit -s -m y',
        "git commit -m x <<EOF\nEOF\ngit commit -s",
    ],
)
def test_a_signing_commit_is_blocked(command: str) -> None:
    assert state_guard.commit_trailer_reason(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\ngit commit -s\nEOF",
        "cat <<'EOF'\ngit commit -s\nEOF",
        'cat <<"EOF"\ngit commit -s\nEOF',
        "cat <<-EOF\ngit commit -s\n\tEOF",
        "cat <<A <<B\ngit commit -s\nA\ngit commit --signoff\nB\ngit status",
        'echo "git commit -s"',
        "echo 'git commit --signoff'",
        'echo "a && git commit -s"',
        'echo \\" git commit -s',
        "echo '\\' ; git status",
        "git commit -m 'fix -s handling'",
        'git commit -m "a -s b"',
        "git commit -m -s",
        "git commit -F -s",
        "git commit -am x",
        "git commit -S -m x",
        "git commit --no-signoff -m x",
        "git commit --no-verify -m x",
        "git commit --message=x",
        "git commit",
        "git status && echo Signed-off-by: x",
        "git -C",
        "git -C d status -s",
        "git log -s",
        "git push --signoff",
        "git",
        "git-extra commit -s",
        "ls -s",
        "env",
        "",
        "echo 'unterminated && git commit -s",
        "FOO=1",
    ],
)
def test_other_commands_pass(command: str) -> None:
    assert state_guard.commit_trailer_reason(command) is None


C = "git" + " commit"  # the shared command-matching table


COMMAND_TABLE = {
    f"{C} -s -m x": 2,
    f"cd r && {C} -s -m x": 2,
    "git -C repo commit --signoff -m x": 2,
    f"FOO=1 {C} -sm x": 2,
    f"{C} -m 'Signed-off-by: a'": 2,
    f"echo $({C} -s -m x)": 2,
    f"cat > t.py <<'EOF'\nassert check('{C} -s -m x')\nEOF\necho done": 0,
    f"cat > t.py <<EOF\n{C} --signoff\nEOF": 0,
    f"{C} --no-signoff -m 'fix: thing'": 0,
    f"grep -n '{C} -s' file": 0,
    "git log --max-count=1": 0,
}


@pytest.mark.parametrize(("command", "status"), COMMAND_TABLE.items())
def test_the_shared_command_matching_table(command: str, status: int) -> None:
    assert (2 if state_guard.commit_trailer_reason(command) else 0) == status


def test_a_value_flag_hides_only_its_own_value() -> None:
    assert state_guard.commit_trailer_reason("git commit -m -s -s") is not None
    assert state_guard.commit_trailer_reason("git commit -m x -m -s") is None
    assert (
        state_guard.commit_trailer_reason("git commit --message -s --file -s") is None
    )
    assert state_guard.commit_trailer_reason("git commit -C HEAD -c x -t y") is None


def test_a_trailer_can_span_argument_boundaries() -> None:
    assert state_guard.signs_off(["Signed-off-by", ": Example"])


@pytest.mark.parametrize("flag", ["m", "F", "C", "c", "t", "U", "S", "u"])
def test_short_values_hide_the_rest_of_the_cluster(flag: str) -> None:
    assert state_guard.commit_trailer_reason(f"git commit -a{flag}fixes") is None
    assert state_guard.commit_trailer_reason(f"git commit -as{flag}fixes") is not None


@pytest.mark.parametrize("flag", sorted(state_guard.SHORT_VALUES))
def test_short_cluster_consumes_a_separate_value(flag: str) -> None:
    assert state_guard.commit_trailer_reason(f"git commit -a{flag} -s") is None
    assert state_guard.commit_trailer_reason(f"git commit -a{flag} -s -s") is not None


def test_optional_short_values_do_not_consume_the_next_word() -> None:
    assert state_guard.commit_trailer_reason("git commit -S -s") is not None
    assert state_guard.commit_trailer_reason("git commit -u -s") is not None
    assert state_guard.commit_trailer_reason("git commit -m" + "fixes") is None
    assert state_guard.short_signoff("-") == (False, False)


@pytest.mark.parametrize(
    "command",
    [
        "env -u NAME X=1 git commit -s",
        "env --unset NAME git commit -s",
        "env -i --unset=NAME git commit -s",
        "env -C path git commit -s",
        "env --chdir path git commit -s",
        "env -- git commit -s",
        "git --git-dir x commit -s",
        "git --work-tree x commit -s",
        "git --namespace x commit -s",
        "git --config-env x commit -s",
        'bash -c "git commit -s"',
        "sh -c 'git commit -s'",
        "zsh -c 'git commit -s'",
        "env -u NAME /bin/bash -c 'git commit -s'",
        'echo "$(git commit -s)"',
        'echo "`git commit -s`"',
        'echo "$(echo $(git commit -s))"',
    ],
)
def test_wrappers_options_and_quoted_scripts_are_checked(command: str) -> None:
    assert state_guard.commit_trailer_reason(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "env -u NAME",
        "env -i",
        "bash -c",
        "sh -x",
        "zsh -c 'git status'",
        "echo '$(git commit -s)'",
        "echo '`git commit -s`'",
        r'echo "\$(git commit -s)"',
        "bash -c 'echo \"git commit -s\"'",
    ],
)
def test_literal_text_inside_quotes_stays_allowed(command: str) -> None:
    assert state_guard.commit_trailer_reason(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "git commit -F message.txt",
        "git commit -F - <<'EOF'\nSigned-off-by: Example\nEOF",
        "git commit -F - <<EOF\nCo-authored-by: Example\nEOF",
    ],
)
def test_message_bodies_are_left_to_the_commit_message_check(command: str) -> None:
    assert state_guard.commit_trailer_reason(command) is None


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("-s", (True, False)),
        ("message", (False, False)),
        ("--amend", (False, False)),
        ("-m", (False, True)),
        ("-m" + "fixes", (False, False)),
        ("-S", (False, False)),
    ],
)
def test_short_option_parser_reports_value_consumption(
    word: str, expected: tuple[bool, bool]
) -> None:
    assert state_guard.short_signoff(word) == expected


def test_scanning_continues_after_a_quoted_substitution() -> None:
    assert (
        state_guard.commit_trailer_reason('echo "$(git status)"; git commit -s')
        is not None
    )
    assert (
        state_guard.commit_trailer_reason('echo "$(git status) $(git commit -s)"')
        is not None
    )


def test_payload_with_no_usable_bash_command_passes() -> None:
    assert (
        state_guard.pre_tool_reason({"tool_name": "Bash", "tool_input": None}) is None
    )
