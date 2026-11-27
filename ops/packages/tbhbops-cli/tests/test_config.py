"""Tests for the environment and the tbhbdev config."""

import re
from pathlib import Path, PurePosixPath

import pytest
from tbhbops_cli import config
from tbhbops_cli.common import CliError

ENV = {
    "OP_CONNECT_TOKEN": "op-secret",
    "TBHBOPS_GITHUB_TOKEN": "gh-secret",
    "OP_CONNECT_HOST": "https://connect.example",
}
GOOD = """\
[gitleaks]
owners = ["ex-org", "ex-user"]

[op]
account = "acct.example.com"

[connect]
server = "srv"
server_hosts = ["op-connect"]
"""


def test_all_the_settings_are_read() -> None:
    assert config.credentials(ENV) == config.Credentials(
        "op-secret", "gh-secret", "https://connect.example"
    )


def test_the_environment_names_are_the_documented_ones() -> None:
    assert [name for name, _ in config.ENVIRONMENT] == [
        "OP_CONNECT_TOKEN",
        "TBHBOPS_GITHUB_TOKEN",
        "OP_CONNECT_HOST",
    ]


@pytest.mark.parametrize("name", ENV)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_setting_is_named_and_no_value_is_printed(
    name: str, value: str | None
) -> None:
    env = {k: v for k, v in ENV.items() if k != name}
    if value is not None:
        env[name] = value
    with pytest.raises(CliError) as raised:
        config.credentials(env)
    message = str(raised.value)
    assert message.startswith(f"missing from the environment: {name} (")
    assert [n for n, _ in config.ENVIRONMENT if n in message] == [name]
    assert "@tbhb runs this command under their own session until they exist" in message
    assert not any(v in message for v in ENV.values())


def test_every_missing_setting_is_named() -> None:
    with pytest.raises(CliError) as raised:
        config.credentials({})
    message = str(raised.value)
    assert all(name in message for name, _ in config.ENVIRONMENT)
    assert message.count("; ") == len(config.ENVIRONMENT) - 1


def test_the_config_is_under_xdg_config_home(tmp_path: Path) -> None:
    got = config.config_path({"XDG_CONFIG_HOME": str(tmp_path)})
    assert got == tmp_path / "tbhbdev" / "config.toml"


@pytest.mark.parametrize("env", [{}, {"XDG_CONFIG_HOME": ""}])
def test_the_config_falls_back_to_dot_config(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config.config_path(env) == tmp_path / ".config" / "tbhbdev" / "config.toml"


def write(tmp_path: Path, text: str) -> dict[str, str]:
    path = tmp_path / "tbhbdev" / "config.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"XDG_CONFIG_HOME": str(tmp_path)}


def test_the_settings_are_read(tmp_path: Path) -> None:
    assert config.settings(write(tmp_path, GOOD)) == config.Settings(
        owners=("ex-org", "ex-user"),
        session_sockets=(),
        account="acct.example.com",
        server="srv",
        server_hosts=("op-connect",),
        workspace=None,
    )


def test_the_optional_settings_are_read(tmp_path: Path) -> None:
    text = (
        GOOD.replace("[gitleaks]\n", '[gitleaks]\nsession_sockets = ["/s/a", "/s/b"]\n')
        + '\n[workspace]\nroot = "Code/ex"\n'
    )
    got = config.settings(write(tmp_path, text))
    assert got.session_sockets == ("/s/a", "/s/b")
    assert got.workspace == PurePosixPath("Code/ex")


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(CliError) as raised:
        config.settings({"XDG_CONFIG_HOME": str(tmp_path)})
    assert (
        str(raised.value) == f"{tmp_path}/tbhbdev/config.toml doesn't exist; create it"
    )


def test_a_file_that_is_not_toml_is_refused_without_quoting_it(tmp_path: Path) -> None:
    env = write(tmp_path, "owners = secret-value\n")
    with pytest.raises(CliError) as raised:
        config.settings(env)
    assert "cannot read" in str(raised.value)
    assert "secret-value" not in str(raised.value)
    assert raised.value.__suppress_context__


def test_a_directory_in_place_of_the_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "tbhbdev" / "config.toml").mkdir(parents=True)
    with pytest.raises(CliError, match="as TOML"):
        config.settings({"XDG_CONFIG_HOME": str(tmp_path)})


def test_bytes_that_are_not_text_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "tbhbdev" / "config.toml"
    path.parent.mkdir()
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(CliError, match="as TOML"):
        config.settings({"XDG_CONFIG_HOME": str(tmp_path)})


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("[gitleaks]\nowners", "[gitleaks]\nother", "[gitleaks] owners must be"),
        ('owners = ["ex-org", "ex-user"]', "owners = []", "[gitleaks] owners must be"),
        ('owners = ["ex-org", "ex-user"]', 'owners = ["a", ""]', "owners must be"),
        ('owners = ["ex-org", "ex-user"]', 'owners = ["a", 1]', "owners must be"),
        ('owners = ["ex-org", "ex-user"]', 'owners = "a"', "owners must be"),
        ("[gitleaks]", "gitleaks = 3\n[x]", "gitleaks must be a table"),
        ("[op]\n", "[other]\n", "[op] account must be a non-empty string"),
        ('account = "acct.example.com"', 'account = ""', "[op] account must be"),
        ('account = "acct.example.com"', "account = 3", "[op] account must be"),
        ('server = "srv"', 'server = ""', "[connect] server must be"),
        ('server_hosts = ["op-connect"]', "server_hosts = []", "server_hosts must be"),
    ],
)
def test_a_bad_setting_is_named(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    env = write(tmp_path, GOOD.replace(old, new))
    with pytest.raises(CliError, match=re.escape(message)) as raised:
        config.settings(env)
    assert str(raised.value).startswith(str(tmp_path / "tbhbdev" / "config.toml"))


@pytest.mark.parametrize(
    ("hosts", "number"),
    [
        ('["-bad"]', 1),
        ('["ok", "bad.dot"]', 2),
        ('["ok", "fine", "' + "x" * 64 + '"]', 3),
        ('["ok", "bad-"]', 2),
    ],
)
def test_a_server_host_that_is_not_a_dns_label_is_named_by_its_place(
    tmp_path: Path, hosts: str, number: int
) -> None:
    env = write(tmp_path, GOOD.replace('["op-connect"]', hosts))
    with pytest.raises(CliError) as raised:
        config.settings(env)
    assert str(raised.value).endswith(
        f"entry {number} of [connect] server_hosts isn't a DNS label"
    )


@pytest.mark.parametrize(
    "hosts", ['["a"]', '["a-b"]', '["a1"]', '["' + "x" * 63 + '"]']
)
def test_a_dns_label_is_accepted(tmp_path: Path, hosts: str) -> None:
    got = config.settings(write(tmp_path, GOOD.replace('["op-connect"]', hosts)))
    assert len(got.server_hosts) == 1


@pytest.mark.parametrize(
    "extra",
    [
        "session_sockets = []",
        'session_sockets = ["", "a"]',
    ],
)
def test_a_bad_session_socket_list_is_refused(tmp_path: Path, extra: str) -> None:
    env = write(tmp_path, GOOD.replace("[gitleaks]\n", f"[gitleaks]\n{extra}\n"))
    with pytest.raises(CliError, match=r"\[gitleaks\] session_sockets must be"):
        config.settings(env)


@pytest.mark.parametrize(
    "workspace",
    [
        "root = '/abs'",
        "root = '../up'",
        "root = 'a/../b'",
        "root = ''",
        "x = 1",
        "root = 3",
    ],
)
def test_a_bad_workspace_root_is_refused(tmp_path: Path, workspace: str) -> None:
    env = write(tmp_path, GOOD + f"\n[workspace]\n{workspace}\n")
    with pytest.raises(CliError, match=r"\[workspace\] root must be a relative path"):
        config.settings(env)


def test_a_workspace_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    env = write(tmp_path, "workspace = 3\n" + GOOD)
    with pytest.raises(CliError, match="workspace must be a table"):
        config.settings(env)


def message_of(tmp_path: Path, text: str) -> str:
    with pytest.raises(CliError) as raised:
        config.settings(write(tmp_path, text))
    return str(raised.value).replace(str(tmp_path / "tbhbdev" / "config.toml"), "PATH")


def test_a_message_names_the_file_the_table_and_the_key_and_never_a_value(
    tmp_path: Path,
) -> None:
    without_op = GOOD.replace('[op]\naccount = "acct.example.com"\n', "")
    assert message_of(
        tmp_path, "connect = 3\n" + GOOD.split("[connect]", maxsplit=1)[0]
    ) == ("PATH: connect must be a table")
    assert message_of(tmp_path, "op = 3\n" + without_op) == "PATH: op must be a table"
    assert message_of(tmp_path, "workspace = 3\n" + GOOD) == (
        "PATH: workspace must be a table"
    )
    sockets = GOOD.replace("[gitleaks]\n", "[gitleaks]\nsession_sockets = []\n")
    assert message_of(tmp_path, sockets) == (
        "PATH: [gitleaks] session_sockets must be a non-empty list of strings"
    )
    root = GOOD + '\n[workspace]\nroot = "/abs"\n'
    assert message_of(tmp_path, root) == (
        "PATH: [workspace] root must be a relative path below home"
    )
    account = without_op + "\n[op]\naccount = 3\n"
    assert message_of(tmp_path, account) == (
        "PATH: [op] account must be a non-empty string"
    )


def test_the_missing_settings_are_listed_in_order_with_what_each_is_for() -> None:
    with pytest.raises(CliError) as raised:
        config.credentials({})
    what = "; ".join(f"{name} ({use})" for name, use in config.ENVIRONMENT)
    assert str(raised.value) == (
        f"missing from the environment: {what}. @tbhb runs this command under "
        "their own session until they exist"
    )


def test_the_connect_server_is_read_alone(tmp_path: Path) -> None:
    path = tmp_path / "tbhbdev" / "config.toml"
    path.parent.mkdir()
    path.write_text('[connect]\nserver = "prd"\n', encoding="utf-8")
    assert config.connect_server({"XDG_CONFIG_HOME": str(tmp_path)}) == "prd"


def test_a_missing_connect_server_is_named_by_its_place(tmp_path: Path) -> None:
    path = tmp_path / "tbhbdev" / "config.toml"
    path.parent.mkdir()
    path.write_text("[op]\naccount = 'a'\n", encoding="utf-8")
    with pytest.raises(CliError) as raised:
        config.connect_server({"XDG_CONFIG_HOME": str(tmp_path)})
    assert str(raised.value) == f"{path}: [connect] server must be a non-empty string"


def test_the_op_account_is_read_alone(tmp_path: Path) -> None:
    path = tmp_path / "tbhbdev" / "config.toml"
    path.parent.mkdir()
    path.write_text('[op]\naccount = "acct.example.com"\n', encoding="utf-8")
    assert config.op_account({"XDG_CONFIG_HOME": str(tmp_path)}) == "acct.example.com"


def test_a_missing_op_account_is_named_by_its_place(tmp_path: Path) -> None:
    path = tmp_path / "tbhbdev" / "config.toml"
    path.parent.mkdir()
    path.write_text('[connect]\nserver = "prd"\n', encoding="utf-8")
    with pytest.raises(CliError) as raised:
        config.op_account({"XDG_CONFIG_HOME": str(tmp_path)})
    assert str(raised.value) == f"{path}: [op] account must be a non-empty string"


def test_a_host_checking_its_token_needs_only_the_url_and_the_token() -> None:
    env = {config.OP_HOST: "https://h.example", config.OP_TOKEN: "t"}
    assert config.connect_credentials(env, lambda _argv: "") == (
        "https://h.example",
        "t",
    )


def test_the_missing_connect_settings_are_named_with_their_use() -> None:
    with pytest.raises(CliError) as raised:
        config.connect_credentials({config.OP_HOST: "https://h.example"}, lambda _: "")
    message = str(raised.value)
    assert message.startswith(f"missing from the environment: {config.OP_TOKEN} (")
    assert config.GITHUB_TOKEN not in message
    assert "@tbhb" not in message
