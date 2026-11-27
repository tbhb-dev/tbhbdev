"""Tests for install environment."""

import tomllib

import pytest

from tbhbdev.alpha.install import model
from tbhbdev.alpha.install.environment import with_environment


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, '[shell_environment_policy.set]\nSSH_AUTH_SOCK = ""\n'),
        ("", '[shell_environment_policy.set]\nSSH_AUTH_SOCK = ""\n'),
        (
            '# mine\nmodel = "o3"\n\n[mcp_servers.x]\ncommand = "y"\n',
            (
                '# mine\nmodel = "o3"\n\n[mcp_servers.x]\ncommand = "y"\n\n'
                '[shell_environment_policy.set]\nSSH_AUTH_SOCK = ""\n'
            ),
        ),
        (
            '[shell_environment_policy]\ninherit = "core"\n',
            (
                '[shell_environment_policy]\ninherit = "core"\n\n'
                '[shell_environment_policy.set]\nSSH_AUTH_SOCK = ""\n'
            ),
        ),
        (
            'shell_environment_policy = { inherit = "core" }\n',
            (
                'shell_environment_policy = { inherit = "core", '
                'set = {SSH_AUTH_SOCK = ""}}\n'
            ),
        ),
        (
            'shell_environment_policy.set = { A = "1" }\n',
            'shell_environment_policy.set = { A = "1", SSH_AUTH_SOCK = ""}\n',
        ),
        (
            '[shell_environment_policy.set]\nSSH_AUTH_SOCK = "x"\nB = "2"\n',
            '[shell_environment_policy.set]\nSSH_AUTH_SOCK = ""\nB = "2"\n',
        ),
    ],
)
def test_with_environment(before: str | None, after: str) -> None:
    assert with_environment(before) == after
    policy = tomllib.loads(after)["shell_environment_policy"]
    assert policy["set"]["SSH_AUTH_SOCK"] == ""
    assert with_environment(after) == after


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "shell_environment_policy = 3\n",
            "its shell_environment_policy is not a table",
        ),
        (
            "[shell_environment_policy]\nset = 3\n",
            "its shell_environment_policy.set is not a table",
        ),
        ("[x\n", "it is not valid TOML ("),
    ],
)
def test_with_environment_refuses_what_it_cannot_merge(text: str, message: str) -> None:
    with pytest.raises(model.InstallError) as raised:
        with_environment(text)
    assert str(raised.value).startswith(message)
