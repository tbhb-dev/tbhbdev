"""Tests for `tbhbops connect`, against fakes for its inventory, config and server."""

import io
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
lazy from collections.abc import Mapping

import pytest
from tbhbops_cli import cli, config
from tbhbops_cli.common import CliError

OP_URL = "https://op-fake.test"
OP_TOKEN = "op-token-value"  # noqa: S105
HERE = Path(__file__).parent
NOW = datetime(2026, 11, 8, 14, 21, 0, tzinfo=UTC)
HOSTS = """\
all:
  children:
    operator_macs:
      hosts:
        ex-ctl1:
        ex-ctl2:
    lab:
      hosts:
        ex-lab1:
    windows:
      hosts:
        ex-win1:
    ci:
      hosts:
        ex-lab1-ci:
        ex-orphan-ci:
        ex-bare:
    agent_hosts:
      children:
        operator_macs:
        lab:
        windows:
    other:
      hosts:
        not-agent:
"""
ALL_VAULTS = [
    "host-0-shared",
    "host-ex-ctl1",
    "host-ex-ctl2",
    "host-ex-lab1",
    "host-ex-win1",
]


@dataclass
class Fake:
    """A fake Connect server, and a record of what it was asked."""

    vaults: list[str] = field(default_factory=list)
    status: int = 200
    create_status: int = 200
    delete_status: int = 204
    created: object = field(default_factory=lambda: {"id": "item-1"})
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    bodies: list[object] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, headers["Authorization"]))
        self.bodies.append(None if body is None else json.loads(body))
        if method == "POST":
            raw = (
                self.created
                if isinstance(self.created, bytes)
                else json.dumps(self.created).encode()
            )
            return self.create_status, raw
        if method == "DELETE":
            return self.delete_status, b""
        assert body is None
        listed = [{"id": self.ids.get(n, f"id-{n}"), "name": n} for n in self.vaults]
        return self.status, json.dumps(listed).encode()

    def probes(self) -> list[tuple[str, str]]:
        """The method and path of each call that wasn't the vault listing."""
        paths = [(m, u.removeprefix(OP_URL)) for m, u, _ in self.calls]
        return [c for c in paths if c != ("GET", "/v1/vaults")]


@dataclass
class World:
    system: cli.System
    fake: Fake

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.run(["connect", *argv], self.system, out, err)
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def world(tmp_path: Path) -> World:
    inventory = tmp_path / "checkout" / "ops" / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts.yml").write_text(HOSTS, encoding="utf-8")
    xdg = tmp_path / "xdg" / "tbhbdev"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text(
        '[connect]\nserver = "srv"\n\n[op]\naccount = "acct.example.com"\n',
        encoding="utf-8",
    )
    fake = Fake()
    env = {
        "TBHB_PRIVATE_ROOT": str(tmp_path / "checkout"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        config.OP_HOST: OP_URL,
        config.OP_TOKEN: OP_TOKEN,
    }

    def no_run(_argv: list[str]) -> str:
        message = "no command runs"
        raise AssertionError(message)

    system = cli.System(
        env, fake, no_run, tmp_path / "checkout", tmp_path / "words", NOW, "ex-lab1"
    )
    return World(system, fake)


def test_token_commands_prints_the_golden_script_for_every_agent_host(
    world: World,
) -> None:
    code, out, err = world.run("token-commands")
    assert (code, err) == (0, "")
    assert out == (HERE / "golden_token_commands.txt").read_text(encoding="utf-8")
    assert world.fake.calls == []


def test_token_commands_dry_run_prints_the_echoed_script(world: World) -> None:
    code, out, err = world.run("token-commands", "--dry-run")
    assert (code, err) == (0, "")
    assert out == (HERE / "golden_token_commands_dry_run.txt").read_text(
        encoding="utf-8"
    )


def test_ops_session_adds_the_session_section_for_that_operator_mac(
    world: World,
) -> None:
    code, plain, err = world.run("token-commands", "--host", "ex-lab1")
    assert (code, err) == (0, "")
    assert "ops-session" not in plain
    code, out, err = world.run(
        "token-commands", "--host", "ex-lab1", "--ops-session", "ex-ctl2"
    )
    assert (code, err) == (0, "")
    assert "# ops-session ex-ctl2\n" in out
    assert "# ops-session ex-ctl1" not in out
    assert "ops-session:ex-ctl2:20261108T142100Z" in out
    assert "host-ops" not in out
    assert "--vault host-ex-ctl1\n" in out


def test_ops_session_dry_run_echoes_the_session_section(world: World) -> None:
    code, out, err = world.run(
        "token-commands", "--host", "ex-lab1", "--ops-session", "ex-ctl2", "--dry-run"
    )
    assert (code, err) == (0, "")
    assert "# ops-session ex-ctl2\n" in out
    assert "# Live, it prepares your keychain ops-connect.keychain on ex-ctl2:\n" in out


def test_ops_session_for_a_host_that_is_no_operator_mac_is_refused(
    world: World,
) -> None:
    code, out, err = world.run("token-commands", "--ops-session", "ex-lab1")
    assert (code, out) == (1, "")
    assert err == (
        "tbhbops connect token-commands: not an operator Mac in the inventory:"
        " ex-lab1\n"
    )


def test_host_narrows_the_tokens_and_may_repeat(world: World) -> None:
    _, out, _ = world.run("token-commands", "--host", "ex-win1", "--host", "ex-lab1")
    assert [line for line in out.splitlines() if line.startswith("# ex-")] == [
        "# ex-lab1",
        "# ex-win1",
    ]
    assert "agent-host:ex-ctl1" not in out


def test_a_host_outside_agent_hosts_is_refused_and_nothing_is_printed(
    world: World,
) -> None:
    code, out, err = world.run("token-commands", "--host", "not-agent")
    assert (code, out) == (1, "")
    assert err == (
        "tbhbops connect token-commands: "
        "not an agent host in the inventory: not-agent\n"
    )


def test_token_commands_without_the_config_names_the_file(
    world: World, tmp_path: Path
) -> None:
    (tmp_path / "xdg" / "tbhbdev" / "config.toml").unlink()
    code, out, err = world.run("token-commands")
    assert (code, out) == (1, "")
    assert err.startswith("tbhbops connect token-commands: ")
    assert "config.toml doesn't exist" in err


def test_token_commands_without_the_op_account_names_the_key(
    world: World, tmp_path: Path
) -> None:
    (tmp_path / "xdg" / "tbhbdev" / "config.toml").write_text(
        '[connect]\nserver = "srv"\n', encoding="utf-8"
    )
    code, out, err = world.run("token-commands")
    assert (code, out) == (1, "")
    assert err.endswith("[op] account must be a non-empty string\n")


def test_check_marks_a_host_whose_token_reaches_exactly_its_vaults_ok(
    world: World,
) -> None:
    world.fake.vaults = ["host-0-shared", "host-ex-lab1"]
    code, out, err = world.run("check")
    assert (code, err) == (0, "")
    assert out == "ex-lab1\n  ok      host-0-shared\n  ok      host-ex-lab1\n"
    assert world.fake.calls == [("GET", f"{OP_URL}/v1/vaults", f"Bearer {OP_TOKEN}")]


def test_check_reports_missing_and_extra_vaults_and_exits_1(world: World) -> None:
    world.fake.vaults = ["host-0-shared", "host-ex-ctl1"]
    code, out, err = world.run("check")
    assert (code, err) == (1, "")
    assert out == (
        "ex-lab1\n  ok      host-0-shared\n  extra   host-ex-ctl1\n  missing host-ex-lab1\n"
    )


@pytest.mark.parametrize("configured", [None, ""])
def test_check_derives_the_full_connect_host_when_unset(
    world: World, configured: str | None
) -> None:
    env = dict(world.system.env)
    del env[config.OP_HOST]
    if configured is not None:
        env[config.OP_HOST] = configured
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str:
        calls.append(argv)
        return json.dumps({"MagicDNSSuffix": "example.example.invalid"})

    world.system = replace(world.system, env=env, run=run)
    world.fake.vaults = ["host-0-shared", "host-ex-lab1"]
    code, out, err = world.run("check")
    assert (code, err) == (0, "")
    assert out == "ex-lab1\n  ok      host-0-shared\n  ok      host-ex-lab1\n"
    assert calls == [["tailscale", "status", "--json"]]
    assert world.fake.calls == [
        ("GET", "https://op-connect.example.example.invalid/v1/vaults", f"Bearer {OP_TOKEN}")
    ]


@pytest.mark.parametrize(
    "status",
    [None, "{}", "[]", '{"MagicDNSSuffix": ""}', '{"MagicDNSSuffix": 3}', "bad JSON"],
)
def test_check_requires_a_full_connect_host_when_derivation_fails(
    world: World, status: str | None
) -> None:
    env = {k: v for k, v in world.system.env.items() if k != config.OP_HOST}
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str:
        calls.append(argv)
        if status is None:
            message = "tailscale failed: private command output"
            raise CliError(message)
        return status

    world.system = replace(world.system, env=env, run=run)
    code, out, err = world.run("check")
    assert (code, out) == (1, "")
    assert calls == [["tailscale", "status", "--json"]]
    assert world.fake.calls == []
    assert err == (
        "tbhbops connect check: cannot derive the Connect URL; "
        "set OP_CONNECT_HOST to the full name, https://op-connect.<MagicDNS suffix>\n"
    )


def test_check_exits_1_for_a_missing_vault_alone_and_for_an_extra_one_alone(
    world: World,
) -> None:
    world.fake.vaults = ["host-0-shared"]
    assert world.run("check")[0] == 1
    world.fake.vaults = ["host-0-shared", "host-ex-lab1", "stray"]
    assert world.run("check")[0] == 1


def test_check_for_a_controller_expects_every_agent_vault(world: World) -> None:
    world.fake.vaults = ALL_VAULTS
    code, out, _ = world.run("check", "--host", "ex-ctl1")
    assert code == 0
    assert out.splitlines()[0] == "ex-ctl1"
    assert out.count("  ok      ") == len(ALL_VAULTS)


def test_check_probes_a_write_in_each_read_write_vault_and_prints_it(
    world: World,
) -> None:
    world.fake.vaults = ALL_VAULTS
    code, out, err = world.run("check", "--host", "ex-ctl1")
    assert (code, err) == (0, "")
    assert out == (
        "ex-ctl1\n"
        "  ok      host-0-shared\n"
        "  ok      host-ex-ctl1\n"
        "  ok      host-ex-ctl2\n"
        "  ok      host-ex-lab1\n"
        "  ok      host-ex-win1\n"
        "host-0-shared write ok\n"
    )
    assert world.fake.probes() == [
        ("POST", "/v1/vaults/id-host-0-shared/items"),
        ("DELETE", "/v1/vaults/id-host-0-shared/items/item-1"),
    ]
    assert world.fake.calls[1][2] == f"Bearer {OP_TOKEN}"
    create = world.fake.bodies[1]
    assert create == {
        "vault": {"id": "id-host-0-shared"},
        "title": "connect-check-probe-20261108T142100Z",
        "category": "SECURE_NOTE",
        "fields": [
            {
                "id": "notesPlain",
                "type": "STRING",
                "purpose": "NOTES",
                "value": "written and deleted by tbhbops connect check",
            }
        ],
    }
    assert world.fake.bodies[2] is None


def test_check_exits_1_when_the_write_probe_is_denied_and_deletes_nothing(
    world: World,
) -> None:
    world.fake.vaults = ALL_VAULTS
    world.fake.create_status = 403
    code, out, err = world.run("check", "--host", "ex-ctl1")
    assert (code, err) == (1, "")
    assert out.endswith("  ok      host-ex-win1\nhost-0-shared write denied (403)\n")
    assert world.fake.probes() == [("POST", "/v1/vaults/id-host-0-shared/items")]


def test_check_exits_1_and_names_the_item_when_the_probe_cleanup_fails(
    world: World,
) -> None:
    world.fake.vaults = ALL_VAULTS
    world.fake.delete_status = 500
    code, out, err = world.run("check", "--host", "ex-ctl1")
    assert (code, err) == (1, "")
    assert out.endswith(
        "host-0-shared write ok, cleanup FAILED: delete item item-1 by hand\n"
    )
    assert world.fake.probes()[-1] == ("DELETE", "/v1/vaults/id-host-0-shared/items/item-1")


def test_check_quotes_every_reserved_character_in_the_probe_ids(world: World) -> None:
    world.fake.vaults = ALL_VAULTS
    world.fake.ids = {"host-0-shared": "v/1"}
    world.fake.created = {"id": "i/2"}
    assert world.run("check", "--host", "ex-ctl1")[0] == 0
    assert world.fake.probes() == [
        ("POST", "/v1/vaults/v%2F1/items"),
        ("DELETE", "/v1/vaults/v%2F1/items/i%2F2"),
    ]


@pytest.mark.parametrize("status", [199, 300, 403, 500])
def test_check_calls_only_a_2xx_create_a_write(world: World, status: int) -> None:
    world.fake.vaults = ALL_VAULTS
    world.fake.create_status = status
    _, out, _ = world.run("check", "--host", "ex-ctl1")
    assert out.endswith(f"host-0-shared write denied ({status})\n")


@pytest.mark.parametrize("status", [199, 300, 404])
def test_check_calls_only_a_2xx_delete_a_cleanup(world: World, status: int) -> None:
    world.fake.vaults = ALL_VAULTS
    world.fake.delete_status = status
    _, out, _ = world.run("check", "--host", "ex-ctl1")
    assert out.endswith("cleanup FAILED: delete item item-1 by hand\n")


@pytest.mark.parametrize("created", [{"title": "x"}, {"id": ""}, {"id": 7}, [1], "x"])
def test_check_names_the_probe_by_title_when_connect_gives_no_item_id(
    world: World, created: object
) -> None:
    world.fake.vaults = ALL_VAULTS
    world.fake.created = created
    code, out, _ = world.run("check", "--host", "ex-ctl1")
    assert code == 1
    assert out.endswith(
        "host-0-shared write ok, cleanup FAILED: "
        "delete item connect-check-probe-20261108T142100Z by hand\n"
    )
    assert [m for m, _ in world.fake.probes()] == ["POST"]


def test_check_names_the_probe_by_title_when_connect_answers_with_no_json(
    world: World,
) -> None:
    world.fake.vaults = ALL_VAULTS
    world.fake.created = b"not json"
    code, out, _ = world.run("check", "--host", "ex-ctl1")
    assert code == 1
    assert out.endswith(
        "host-0-shared write ok, cleanup FAILED: "
        "delete item connect-check-probe-20261108T142100Z by hand\n"
    )


def test_check_does_not_probe_a_read_write_vault_the_token_does_not_reach(
    world: World,
) -> None:
    world.fake.vaults = [v for v in ALL_VAULTS if v != "host-0-shared"]
    code, out, _ = world.run("check", "--host", "ex-ctl1")
    assert code == 1
    assert "  missing host-0-shared\n" in out
    assert "write" not in out
    assert world.fake.probes() == []


def test_check_never_probes_a_host_with_no_read_write_grant(world: World) -> None:
    world.fake.vaults = ["host-0-shared", "host-ex-lab1"]
    code, out, _ = world.run("check")
    assert code == 0
    assert "write" not in out
    assert world.fake.probes() == []


def test_check_never_probes_an_ops_controller_token(world: World) -> None:
    world.fake.vaults = OPS_VAULTS
    code, out, _ = world.run("check", "--ops-controller", "--host", "ex-ctl1")
    assert code == 0
    assert "write" not in out
    assert world.fake.probes() == []


def test_check_never_probes_the_read_only_vaults_of_a_controller(
    world: World,
) -> None:
    world.fake.vaults = ALL_VAULTS
    world.run("check", "--host", "ex-ctl1")
    assert all("id-host-0-shared" in path for _, path in world.fake.probes())


OPS_VAULTS = ["host-storage-nas-a", "host-storage-nas-b"]


def test_host_narrows_the_ops_controller_tokens_to_the_operator_macs_chosen(
    world: World,
) -> None:
    _, lab, _ = world.run("token-commands", "--host", "ex-lab1")
    assert "ops-controller" not in lab
    _, ctl, _ = world.run("token-commands", "--host", "ex-ctl2")
    assert "# ops-controller ex-ctl2\n" in ctl
    assert "# ops-controller ex-ctl1" not in ctl


def test_check_ops_controller_marks_exactly_the_host_ops_vaults_ok(
    world: World,
) -> None:
    world.fake.vaults = OPS_VAULTS
    code, out, err = world.run("check", "--ops-controller", "--host", "ex-ctl1")
    assert (code, err) == (0, "")
    assert (
        out == "ex-ctl1\n  ok      host-storage-nas-a\n  ok      host-storage-nas-b\n"
    )


def test_check_ops_controller_defaults_to_this_host(world: World) -> None:
    world.system = replace(world.system, hostname="ex-ctl2")
    world.fake.vaults = OPS_VAULTS
    code, out, _ = world.run("check", "--ops-controller")
    assert code == 0
    assert out.splitlines()[0] == "ex-ctl2"


def test_check_ops_controller_reports_a_missing_and_an_extra_vault(
    world: World,
) -> None:
    world.fake.vaults = ["host-0-shared", "host-storage-nas-b"]
    code, out, _ = world.run("check", "--ops-controller", "--host", "ex-ctl1")
    assert code == 1
    assert out == (
        "ex-ctl1\n  extra   host-0-shared\n  missing host-storage-nas-a\n"
        "  ok      host-storage-nas-b\n"
    )


def test_check_ops_controller_refuses_a_host_that_is_not_an_operator_mac(
    world: World,
) -> None:
    code, out, err = world.run("check", "--ops-controller", "--host", "ex-lab1")
    assert (code, out) == (1, "")
    assert err == (
        "tbhbops connect check: not an operator Mac in the inventory: ex-lab1\n"
    )
    assert world.fake.calls == []


def test_check_without_the_flag_still_expects_the_agent_host_vaults(
    world: World,
) -> None:
    world.fake.vaults = OPS_VAULTS
    code, out, _ = world.run("check", "--host", "ex-ctl1")
    assert code == 1
    assert "  extra   host-storage-nas-a\n" in out
    assert "  missing host-0-shared\n" in out


def test_check_with_no_vaults_reports_them_all_missing(world: World) -> None:
    code, out, _ = world.run("check")
    assert code == 1
    assert out == "ex-lab1\n  missing host-0-shared\n  missing host-ex-lab1\n"


def test_check_never_prints_the_token_or_the_url(world: World) -> None:
    world.fake.vaults = ["stray"]
    _, out, err = world.run("check")
    for text in (out, err):
        assert OP_TOKEN not in text
        assert "op-fake.test" not in text


def test_check_refuses_a_host_that_is_not_an_agent_host(world: World) -> None:
    code, out, err = world.run("check", "--host", "not-agent")
    assert (code, out) == (1, "")
    assert err == (
        "tbhbops connect check: not an agent host in the inventory: not-agent\n"
    )
    assert world.fake.calls == []


def test_check_reads_the_inventory_named_by_inventory_from_any_directory(
    world: World, tmp_path: Path
) -> None:
    named = tmp_path / "checkout" / "ops" / "ansible" / "inventory"
    elsewhere = tmp_path / "home" / "agent"
    elsewhere.mkdir(parents=True)
    env = {key:value for key,value in world.system.env.items() if key != "TBHB_PRIVATE_ROOT"}
    world.system = replace(world.system, cwd=elsewhere, env=env)
    code, _, err = world.run("check")
    assert code == 1
    assert "no ansible/inventory/hosts.yml in" in err
    world.fake.vaults = ["host-0-shared", "host-ex-lab1"]
    code, out, err = world.run("check", "--inventory", str(named))
    assert (code, err) == (0, "")
    assert out.startswith("ex-lab1\n  ok      host-0-shared\n")


def test_check_refuses_an_inventory_without_hosts_yml(
    world: World, tmp_path: Path
) -> None:
    code, out, err = world.run("check", "--inventory", str(tmp_path))
    assert (code, out) == (1, "")
    assert err == f"tbhbops connect check: no hosts.yml in {tmp_path}\n"
    assert world.fake.calls == []


def test_check_needs_only_the_url_and_the_token_in_the_environment(
    world: World,
) -> None:
    env = {k: v for k, v in world.system.env.items() if k != config.OP_TOKEN}
    system = cli.System(
        env,
        world.system.http,
        world.system.run,
        world.system.cwd,
        world.system.words,
        NOW,
        "ex-lab1",
    )
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["connect", "check"], system, out, err) == 1
    assert err.getvalue().startswith(
        f"tbhbops connect check: missing from the environment: {config.OP_TOKEN} ("
    )


def test_check_reports_a_failed_listing_by_status_alone(world: World) -> None:
    world.fake.status = 403
    code, out, err = world.run("check")
    assert (code, out) == (1, "")
    assert err == "tbhbops connect check: listing the vaults failed: HTTP 403\n"


def help_of(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> str:
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        cli.parser().parse_args([*argv, "--help"])
    return " ".join(capsys.readouterr().out.split())


def test_the_connect_help_says_what_each_command_is_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    top = help_of(monkeypatch, capsys)
    assert "connect the agent hosts' Connect tokens" in top
    group = help_of(monkeypatch, capsys, "connect")
    assert group.startswith("usage: tbhbops connect ")
    assert (
        "token-commands print the shell script @tbhb runs to create the agent hosts'"
        " tokens"
    ) in group
    assert "check check that this host's token reaches the vaults it should" in group


def test_the_token_commands_help_says_what_it_prints_and_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shown = help_of(monkeypatch, capsys, "connect", "token-commands")
    assert shown.startswith("usage: tbhbops connect token-commands ")
    assert (
        "Print, and never run, a POSIX shell script that grants the Connect server its"
        " vaults and creates one token with no expiry for each agent host. The server"
        " comes from [connect] server in the tbhbdev config, and the hosts from the"
        " agent_hosts group of the inventory."
    ) in shown
    assert (
        "--host HOST make a token for only this agent host; repeat it for more" in shown
    )
    assert (
        "--ops-session HOST also make an ops-session token for the operator's agent"
        " session on this operator Mac, and keychain it as the user who runs the"
        " script; off unless given, and it takes a host so the Mac is never guessed"
    ) in shown
    assert (
        "--runner-vm HOST make a runner-vm token for this CI runner VM, which reads"
        " only the vault runner-fixture, and store it in its lab node's vault as"
        " runner-access-token; repeat it for more. Without --host and --ops-session, the"
        " script makes only these tokens; with either, it makes theirs too"
    ) in shown
    assert (
        "--dry-run print the same script with each op command echoed instead of run"
        in shown
    )


def test_the_check_help_says_what_it_compares_and_when_it_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shown = help_of(monkeypatch, capsys, "connect", "check")
    assert shown.startswith("usage: tbhbops connect check ")
    assert (
        "List the vaults the token in OP_CONNECT_TOKEN reaches, and compare them with"
        " the vaults the script grants the host. A vault the token lacks is missing,"
        " and one it shouldn't reach is extra; either one exits 1."
    ) in shown
    assert (
        "--host HOST the agent host to check; default this host's short name" in shown
    )
    assert (
        "--ops-controller check an ops-controller token, which reads exactly the"
        " host-ops vaults" in shown
    )
    assert (
        "--inventory INVENTORY the inventory directory, which holds hosts.yml; default"
        " the ops/ansible/inventory of the private companion" in shown
    )


def test_the_connect_commands_parse_their_arguments() -> None:
    parse = cli.parser().parse_args
    made = parse(["connect", "token-commands"])
    assert (made.group, made.command, made.host, made.dry_run) == (
        "connect",
        "token-commands",
        [],
        False,
    )
    assert made.ops_session is None
    assert parse(["connect", "token-commands", "--ops-session", "a"]).ops_session == "a"
    made = parse(
        ["connect", "token-commands", "--host", "a", "--host", "b", "--dry-run"]
    )
    assert (made.host, made.dry_run) == (["a", "b"], True)
    checked = parse(["connect", "check"])
    assert (checked.command, checked.host, checked.ops_controller) == (
        "check",
        None,
        False,
    )
    assert parse(["connect", "check", "--ops-controller"]).ops_controller is True
    assert parse(["connect", "check", "--host", "a"]).host == "a"
    assert checked.inventory is None
    assert parse(["connect", "check", "--inventory", "d"]).inventory == Path("d")
    bad = [["connect"], ["connect", "check", "--dry-run"], ["connect", "token"]]
    for argv in bad:
        with pytest.raises(SystemExit):
            parse(argv)


def test_the_real_system_names_the_host_in_lowercase_without_its_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "MAC-B.sub.example.invalid")
    assert cli.real_system().hostname == "mac-b"
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "Plain")
    assert cli.real_system().hostname == "plain"


def test_the_real_system_reads_the_clock_in_utc() -> None:
    before = datetime.now(UTC)
    now = cli.real_system().now
    assert now.utcoffset() == timedelta(0)
    assert before <= now <= datetime.now(UTC)


def test_runner_vm_adds_the_runner_vm_section_for_that_vm(world: World) -> None:
    _, plain, _ = world.run("token-commands", "--host", "ex-lab1")
    assert "runner-vm" not in plain
    code, out, err = world.run(
        "token-commands", "--host", "ex-lab1", "--runner-vm", "ex-lab1-ci"
    )
    assert (code, err) == (0, "")
    assert "# runner-vm ex-lab1-ci\n" in out
    assert "runner-vm:ex-lab1-ci:20261108T142100Z" in out
    assert "--vault runner-fixture,r)" in out
    assert "--runner-vm" not in out
    assert "host-ex-lab1 already holds runner-access-token; skipping" in out


def test_runner_vm_alone_prints_only_the_runner_vm_script(world: World) -> None:
    code, out, err = world.run("token-commands", "--runner-vm", "ex-lab1-ci")
    assert (code, err) == (0, "")
    assert "# runner-vm ex-lab1-ci\n" in out
    assert "agent-host" not in out
    assert "host-0-shared" not in out
    assert "ops-controller" not in out
    assert "--vault host-ex-ctl1" not in out
    _, session, _ = world.run(
        "token-commands", "--runner-vm", "ex-lab1-ci", "--ops-session", "ex-ctl2"
    )
    assert "agent-host:ex-ctl1:" in session
    assert "# runner-vm ex-lab1-ci\n" in session


def test_without_runner_vm_the_inventory_needs_no_ci_group(
    world: World, tmp_path: Path
) -> None:
    hosts = tmp_path / "checkout" / "ops" / "ansible" / "inventory" / "hosts.yml"
    ci = "    ci:\n      hosts:\n        ex-lab1-ci:\n        ex-orphan-ci:\n"
    ci += "        ex-bare:\n"
    hosts.write_text(HOSTS.replace(ci, ""), encoding="utf-8")
    code, out, err = world.run("token-commands")
    assert (code, err) == (0, "")
    assert out == (HERE / "golden_token_commands.txt").read_text(encoding="utf-8")


def test_runner_vm_defaults_to_none_and_repeats() -> None:
    parser = cli.parser()
    plain = parser.parse_args(["connect", "token-commands"])
    assert plain.runner_vm == []
    two = parser.parse_args(
        ["connect", "token-commands", "--runner-vm", "a-ci", "--runner-vm", "b-ci"]
    )
    assert two.runner_vm == ["a-ci", "b-ci"]


def test_runner_vm_dry_run_echoes_the_runner_vm_section(world: World) -> None:
    code, out, err = world.run(
        "token-commands", "--host", "ex-lab1", "--runner-vm", "ex-lab1-ci", "--dry-run"
    )
    assert (code, err) == (0, "")
    assert "# runner-vm ex-lab1-ci\n" in out
    assert "echo 'op item get --account acct.example.com runner-access-token" in out


NO_VAULT = (
    "the lab node of {} is not an agent host in the inventory:"
    " its token has no vault to go in"
)


@pytest.mark.parametrize(
    ("vm", "message"),
    [
        ("ex-lab1", "not a CI runner VM in the inventory: ex-lab1"),
        ("ex-orphan-ci", NO_VAULT.format("ex-orphan-ci")),
        ("ex-bare", NO_VAULT.format("ex-bare")),
    ],
)
def test_a_runner_vm_that_cannot_hold_its_token_is_refused(
    world: World, vm: str, message: str
) -> None:
    code, out, err = world.run("token-commands", "--runner-vm", vm)
    assert (code, out) == (1, "")
    assert err == f"tbhbops connect token-commands: {message}\n"
