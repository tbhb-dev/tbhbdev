# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise how file contents expose proposed 1Password invocations."""

import json
from pathlib import Path

import pytest

from tbhbdev.alpha.guard.op import file_calls as guard
from tbhbdev.alpha.guard.op.decision import allowed


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("x.md", "op read x\n```python\nop read x\n```\n"),
        ("x.md", "```console\nop read x\n```\n"),
        ("x.md", "```sh\nop read x --account example.1password.com\n```\n"),
        ("x.py", '["op", "read", ref, "--account", ACCOUNT]'),
        ("x.py", '("op", *args, "--account", account)'),
        ("x.py", '["op", "--account=anything"]'),
        ("x.py", 'log("op read x printed no token")'),
        ("x.py", 'other.run("op read x", shell=True)'),
        ("x.py", 'subprocess.run("op read x")'),
        ("x.py", "os.system(variable)"),
        ("x.py", '["echo", "op"]'),
        ("x.py", "[]\nx = 1\n%magic\n!ignored"),
        ("x.py", "invalid python :"),
        ("x.yaml", "- shell: op item get x --account example.1password.com"),
        ("x.yaml", "argv: [op, --account, anything]\nother: 3"),
        ("x.yaml", "["),
        ("x.txt", "op read x"),
    ],
)
def test_allowed_file(name: str, content: str) -> None:
    assert all(
        allowed(words, "example.1password.com")
        for words in guard.file_calls(Path(name), content)
    )


def test_nested_python_attribute() -> None:
    assert not list(guard.python_calls('client.os.system("op read x")'))


@pytest.mark.parametrize(
    "text",
    [
        'other.system("op read x")',
        'os.other("op read x")',
        'subprocess.run("op read x", other=True)',
        'subprocess.run("op read x", shell=False)',
    ],
)
def test_python_other_calls(text: str) -> None:
    assert not list(guard.python_calls(text))


@pytest.mark.parametrize("tag", ["sh", "bash", "shell", "zsh", "console"])
def test_markdown_blocks(tag: str) -> None:
    source = "op read x\nop read y" if tag != "console" else "$ op read x\n# op read y"
    text = f"```{tag}\n{source}\n```\nprose op read z\n```{tag}\n{source}\n```\n"
    assert (
        list(guard.markdown_calls(text))
        == [["op", "read", "x"], ["op", "read", "y"]] * 2
    )


def test_yaml_prose() -> None:
    assert not list(guard.yaml_calls({"message": "op read x"}))


def test_zsh_file() -> None:
    assert list(guard.file_calls(Path("x.zsh"), "op read x")) == [["op", "read", "x"]]


def test_yaml_other_argv() -> None:
    assert not list(guard.yaml_calls({"argv": ["echo", "op"]}))


def test_unclosed_markdown_fence() -> None:
    assert list(guard.markdown_calls("```sh\nop read x\nop read y")) == [
        ["op", "read", "x"],
        ["op", "read", "y"],
    ]
    assert not list(guard.markdown_calls("```python\nop read x"))


def test_markdown_closing_fence_stops_shell() -> None:
    text = "```sh\nop read x\n```\nop read prose\n```python\nop read python\n```"
    assert list(guard.markdown_calls(text)) == [["op", "read", "x"]]


@pytest.mark.parametrize(
    "info", ["bash title=x", "{.bash}", "Bash", "sh-session", "shellscript"]
)
def test_fence_info(info: str) -> None:
    body = "$ op read x\nop output" if info == "sh-session" else "op read x"
    assert list(guard.markdown_calls(f"```{info}\n{body}\n```")) == [
        ["op", "read", "x"]
    ]


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: prose\n---\ncommand: [op, read, x]",
        "script: [op read x]",
        "before_script: [op read x]",
        "after_script: [op read x]",
        "ansible.builtin.command: [op, read, x]",
    ],
)
def test_yaml_review_shapes(text: str) -> None:
    assert list(guard.file_calls(Path("x.yml"), text)) == [["op", "read", "x"]]


def test_yaml_script_non_string() -> None:
    assert not list(guard.yaml_calls({"script": [123]}))


@pytest.mark.parametrize("info", ["", "XBashX", "{XBashX}"])
def test_non_shell_fence_info(info: str) -> None:
    assert not list(guard.markdown_calls(f"```{info}\nop read x\n```"))


def book(source: str) -> str:
    return json.dumps({"cells": [{"id": "one", "cell_type": "code", "source": source}]})


def test_notebook_magic_options() -> None:
    assert list(guard.notebook_calls(book("%%bash -e\nop read x"))) == [
        ["op", "read", "x"]
    ]


def test_notebook_multiple_cells() -> None:
    cells = [
        {"cell_type": "markdown", "source": "op read z"},
        {"cell_type": "code", "source": ["%%sh\n", "op read x\n"]},
        {"cell_type": "code", "source": "  !op read y\n"},
        {"cell_type": "code"},
    ]
    assert list(guard.notebook_calls(json.dumps({"cells": cells}))) == [
        ["op", "read", "x"],
        ["op", "read", "y"],
    ]


@pytest.mark.parametrize(
    "source",
    [
        "x = !op read x",
        "%%script bash\nop read x",
        "%%script sh\nop read x",
        "%%script zsh\nop read x",
        'subprocess.run("op read x", shell=True)',
    ],
)
def test_notebook_review_shapes(source: str) -> None:
    assert list(guard.notebook_calls(book(source))) == [["op", "read", "x"]]


@pytest.mark.parametrize(
    "source",
    [
        "%%script bash -e\nop read x",
        "%%script sh -e\nop read x",
        "%%script zsh -e\nop read x",
        "X = !op read x",
    ],
)
def test_notebook_magic_arguments(source: str) -> None:
    assert list(guard.notebook_calls(book(source))) == [["op", "read", "x"]]
