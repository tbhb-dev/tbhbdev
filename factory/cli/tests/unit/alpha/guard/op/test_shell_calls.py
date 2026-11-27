# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise how shell calls to 1Password are recognized."""

import pytest

from tbhbdev.alpha.guard.op import shell_calls as guard
from tbhbdev.alpha.guard.op.decision import allowed

ACCOUNT = "example.1password.com"


SHAPES = [
    "op item get x",
    "/opt/homebrew/bin/op item get x",
    "'op' item get x",
    "FOO=1 op item get x",
    "env op item get x",
    "command op item get x",
    "exec op item get x",
    "sudo -u agent op item get x",
    "sudo -g staff op item get x",
    "timeout 30 op item get x",
    "timeout -s TERM 30 op item get x",
    "nice -n 5 op item get x",
    "mise x -- op item get x",
    "mise exec -- op item get x",
    "xargs op item get x",
    "x | op item get x",
    "a && op item get x",
    "a; op item get x",
    "echo $(op item get x{flag})",
    'echo "$(op item get x{flag})"',
    "echo `op item get x{flag}`",
    "sh -c 'op item get x{flag}'",
    'bash -lc "op item get x{flag}"',
    "eval op item get x",
    "ssh -o X=y host 'op item get x{flag}'",
    "bash <<'EOF'\nop item get x{flag}\nEOF\n",
    "ssh host <<'EOF'\nop item get x{flag}\nEOF\n",
    "OP_ACCOUNT=x op item get x",
    "op \\\nitem get x",
    "op item get x \\\n{flag}",
]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("flag", ["", f" --account {ACCOUNT}", f" --account={ACCOUNT}"])
def test_shell(shape: str, flag: str) -> None:
    text = shape.format(flag=flag) if "{flag}" in shape else shape + flag
    assert any(not allowed(words, ACCOUNT) for words in guard.shell_calls(text)) == (
        not flag
    )


@pytest.mark.parametrize(
    "text",
    [
        "opam",
        "echo op",
        'echo "op connect token create printed no token"',
        "git commit -m 'op item get'",
        "grep op file",
        "\n# comment\n",
        "mise x op item get x",
        "env",
        "bash script.sh",
    ],
)
def test_prose(text: str) -> None:
    assert list(guard.shell_calls(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "cat <<EOF\nop read x\nEOF\n",
        "bash -c 'echo hello' <<EOF\nop read x\nEOF\n",
        "ssh host 'echo hello' <<EOF\nop read x\nEOF\n",
        "env <<EOF\nop read x\nEOF\n",
    ],
)
def test_data_heredocs(text: str) -> None:
    assert not list(guard.shell_calls(text))


@pytest.mark.parametrize(
    "text",
    [
        "bash <<op\necho safe\nop\n",
        "bash <<-op\necho safe\n\top\n",
        "bash <<op\necho safe\n",
        "bash <<op\n",
        "bash <<one <<two\necho first\none\necho second\ntwo\n",
    ],
)
def test_heredoc_delimiters_are_data(text: str) -> None:
    assert not list(guard.shell_calls(text))


@pytest.mark.parametrize(
    "text",
    [
        "timeout --signal TERM 20 op read x",
        "nice op read x",
        "sudo --user agent --group staff op read x",
        "env FOO=1 command exec /usr/bin/op read x",
        "ssh -v -p 22 -i key -l user -J jump host op read x",
        "bash -c 'timeout 3 op read x'",
        "mise exec --cd . -- op read x",
    ],
)
def test_extended_prefixes(text: str) -> None:
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]] or list(
        guard.shell_calls(text)
    ) == [["/usr/bin/op", "read", "x"]]


def test_heredoc_pipeline() -> None:
    assert list(guard.shell_calls("bash <<EOF | cat\nop read x\nEOF\n")) == [
        ["op", "read", "x"]
    ]
    assert not list(guard.shell_calls("cat <<EOF | bash\nop read x\nEOF\n"))


@pytest.mark.parametrize(
    "text",
    [
        "/usr/bin/timeout 30 op read x",
        "env -- mise x -- sudo -u agent op read x",
        "FOO=1 sudo -u agent timeout 30 op read x",
        '"o\\\np" read x',
    ],
)
def test_prefix_combinations(text: str) -> None:
    assert any(not allowed(words, ACCOUNT) for words in guard.shell_calls(text))


@pytest.mark.parametrize(
    "text",
    [
        "sudo -u agent bash -c 'op read x'",
        "timeout 30 eval op read x",
        "mise x -- sh -c 'op read x'",
    ],
)
def test_wrapped_shells(text: str) -> None:
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


def test_empty_wrapper_before_call() -> None:
    assert list(guard.shell_calls("timeout 5; mise x --; op read x")) == [
        ["op", "read", "x"]
    ]


@pytest.mark.parametrize("wrapper", ["sh -c", "eval"])
def test_nested_heredocs(wrapper: str) -> None:
    text = f"{wrapper} 'bash <<EOF\nop read x\nEOF\n'"
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ([], []),
        (["op", "read", "x"], [["op", "read", "x"]]),
        (["ssh", "-o", "X=y", "host", "op", "read", "x"], [["op", "read", "x"]]),
        (["ssh", "host", "op read x"], [["op", "read", "x"]]),
        (["ssh", "host"], []),
        (["echo", "op"], []),
    ],
)
def test_direct_routes(words: list[str], expected: list[list[str]]) -> None:
    assert list(guard.direct_calls(words)) == expected


@pytest.mark.parametrize(
    "prefix",
    [
        "sudo -n",
        "sudo -k",
        "sudo -s",
        *[
            f"sudo {flag} value"
            for flag in ("-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U")
        ],
        "nice -n 2",
        "timeout -k 1 5",
        "timeout -s TERM 5",
        "timeout 5 --",
        "env -u NAME",
        "env -C .",
        "env -S 'op read x'",
        "exec -a name",
        *[
            f"xargs {flag}{value}"
            for flag in ("-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a")
            for value in ("value", " value")
        ],
        "ssh -t host --",
        "ssh host -t",
        "ssh host --",
        ">out",
        "> out",
        "2>/dev/null",
        "2> /dev/null",
        "2>&1",
        "2>& 1",
        "<in",
        "< in",
        "&>f",
        "&> f",
        "function f {",
        "find . -exec",
        "find . -execdir",
        "watch",
        "watch -n 2",
        "stdbuf -oL",
        "stdbuf -o L",
        "setsid",
        "ionice -c 3",
        "doas -u user",
        "flock -w 3 file",
    ],
)
def test_review_shell_shapes(prefix: str) -> None:
    text = prefix if prefix == "env -S 'op read x'" else prefix + " op read x"
    if prefix.startswith("find"):
        text += " \\;"
    if prefix == "function f {":
        text += "; }"
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


@pytest.mark.parametrize(
    "text",
    [
        "command -v op",
        "command -V op",
        "env command -v op",
        "echo 'op read x'",
        "env -u NAME",
    ],
)
def test_lookup_exemption(text: str) -> None:
    assert not list(guard.shell_calls(text))


@pytest.mark.parametrize("text", ["op --version", "op signin"])
def test_all_op_commands_need_account(text: str) -> None:
    assert any(not allowed(words, ACCOUNT) for words in guard.shell_calls(text))


@pytest.mark.parametrize(
    "text",
    [
        "find . -exec op read x {} +",
        "find . -execdir sh -c 'op read x' \\;",
        "find . -exec echo safe \\; -exec op read x \\;",
        '>"file name" op read x',
        "<'file name' op read x",
        "env -S 'bash -c' 'op read x'",
    ],
)
def test_recursive_review_shapes(text: str) -> None:
    calls = list(guard.shell_calls(text))
    assert calls
    assert calls[0][:3] == ["op", "read", "x"]


@pytest.mark.parametrize(
    "text",
    [
        "function f op read x",
        "time -p op read x",
        "env -u NAME -S 'op read x'",
        "ssh -- -destination op read x",
        "function f { op read x; }",
        "if function f { op read x; }; then true; fi",
    ],
)
def test_mutation_prefix_regressions(text: str) -> None:
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


@pytest.mark.parametrize("text", ["mise run -- op read x", "echo exec -- op read x"])
def test_only_mise_execution_launchers(text: str) -> None:
    assert not list(guard.shell_calls(text))


@pytest.mark.parametrize(
    "text",
    [
        "find . -exec op read x {} + -print",
        "find . -exec op read x",
        "find -exec op read x \\;",
        "find . -exec \\; -exec op read x \\;",
        "find . -print",
    ],
)
def test_find_clause_boundaries(text: str) -> None:
    expected: list[list[str]] = (
        []
        if text == "find . -print"
        else [["op", "read", "x", "{}"]]
        if "{}" in text
        else [["op", "read", "x"]]
    )
    assert list(guard.shell_calls(text)) == expected
