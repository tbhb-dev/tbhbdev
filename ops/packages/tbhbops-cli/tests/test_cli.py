"""Tests for the command, run against fakes for each thing it touches."""

import io
import json
import os
import re
import stat
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
lazy import argparse
lazy from collections.abc import Mapping
lazy from typing import TextIO

import pytest
from tbhbops import dns_smoke
from tbhbops_cli import cli, config, gitleaks, sources
from tbhbops_cli.common import CliError, https_request

OP_URL = "https://op-fake.test"
OP_TOKEN = "op-token-value"
GH_TOKEN = "gh-token-value"
ENV = {
    config.OP_TOKEN: OP_TOKEN,
    config.GITHUB_TOKEN: GH_TOKEN,
    config.OP_HOST: OP_URL,
}
NOW = datetime(2026, 11, 8, 14, 21, 0, tzinfo=UTC)
SUFFIX = "tail-ex.net"
TAILNET_NAME = "ex.example"
ITEM_ID = "item-1"


@pytest.mark.parametrize("custom", [False, True])
def test_dns_smoke_dispatch(
    world: World, monkeypatch: pytest.MonkeyPatch, *, custom: bool
) -> None:
    def dispatch(
        args: argparse.Namespace, system: dns_smoke.SmokeSystem, out: TextIO
    ) -> int:
        assert system is world.system
        assert args.group == "dns"
        assert args.command == "smoke"
        assert args.source == Path("/custom/source" if custom else "apps/dns")
        assert args.artifacts == Path(
            "/custom/artifacts" if custom else "dns-smoke-artifacts"
        )
        out.write("smoke dispatched\n")
        return 7

    monkeypatch.setattr(dns_smoke, "dispatch", dispatch)
    argv = ["dns", "smoke"]
    if custom:
        argv += [
            "--source",
            "/custom/source",
            "--artifacts",
            "/custom/artifacts",
        ]
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(argv, world.system, out, err) == 7
    assert out.getvalue() == "smoke dispatched\n"
    assert err.getvalue() == ""


@dataclass
class Connect:
    """A fake Connect server, and a record of what it was asked."""

    vaults: dict[str, str] = field(
        default_factory=lambda: {"host-0-shared": "v-all", "ab": "v2"}
    )
    item: dict[str, object] | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    bodies: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        assert headers["Authorization"] == f"Bearer {OP_TOKEN}"
        path = url.removeprefix(OP_URL)
        self.calls.append((method, urllib.parse.unquote(path)))
        if body is not None:
            self.bodies.append(json.loads(body))
        if path == "/v1/vaults":
            return 200, json.dumps(
                [{"id": i, "name": n} for n, i in self.vaults.items()]
            ).encode()
        for vault_id in self.vaults.values():
            prefix = f"/v1/vaults/{vault_id}/items"
            if path.startswith(f"{prefix}?filter="):
                title = (
                    "scanner-policy"
                    if self.item is None
                    else str(self.item.get("title", "scanner-policy"))
                )
                listed: list[dict[str, str]] = (
                    [] if self.item is None else [{"id": ITEM_ID, "title": title}]
                )
                return 200, json.dumps(listed).encode()
            if method == "GET" and path == f"{prefix}/{ITEM_ID}":
                return 200, json.dumps(self.item).encode()
            if method in {"POST", "PUT"} and path.startswith(prefix):
                return 200, b"{}"
        return 404, b""


@dataclass
class World:
    system: cli.System
    connect: Connect
    github_calls: list[bytes] = field(default_factory=list)
    tailscale_calls: list[list[str]] = field(default_factory=list)

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.run(["gitleaks", "generate", *argv], self.system, out, err)
        return code, out.getvalue(), err.getvalue()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    checkout = tmp_path / "checkout"
    inventory = checkout / "ops" / "ansible" / "inventory"
    write(
        inventory / "hosts.yml",
        "all:\n  children:\n    lab:\n      hosts:\n"
        "        ex-lab1:\n        ex-lab2:\n"
        "        ex-lab3:\n    edge:\n      hosts:\n        edge-1:\n",
    )
    write(inventory / "retired-hosts.yml", "retired_hosts: []\n")
    write(inventory / "host_vars" / "edge-1.yml", "ansible_host: ex-edge\n")
    write(inventory / "group_vars" / "all.yml", "agent_account_operator: opr\n")
    write(
        tmp_path / "xdg" / "tbhbdev" / "config.toml",
        '[gitleaks]\nowners = ["ex-org"]\nsession_sockets = ["/tmp/sock/a.sock"]\n'
        '[op]\naccount = "acct.example.com"\n'
        '[connect]\nserver = "srv"\nserver_hosts = ["op-connect"]\n'
        '[workspace]\nroot = "Code/ex"\n',
    )
    write(tmp_path / "words", "alpha\ncommon\nconfig\n")

    def which(_name: str) -> str:
        return "/bin/tailscale"

    monkeypatch.setattr(sources.shutil, "which", which)
    connect = Connect()
    built = World(system=None, connect=connect)  # type: ignore[arg-type]  # set below

    def http(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        if url == sources.GRAPHQL:
            assert headers["Authorization"] == f"Bearer {GH_TOKEN}"
            assert body is not None
            built.github_calls.append(body)
            names = ["secret-thing", "config", "ab", "dots"]
            repositories = {
                "nodes": [{"name": n} for n in names],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            answer = {"data": {"repositoryOwner": {"repositories": repositories}}}
            return 200, json.dumps(answer).encode()
        return connect(method, url, headers, body)

    def run(argv: list[str]) -> str:
        built.tailscale_calls.append(argv)
        return json.dumps(
            {"MagicDNSSuffix": SUFFIX, "CurrentTailnet": {"Name": TAILNET_NAME}}
        )

    env = {
        **ENV,
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "TBHB_PRIVATE_ROOT": str(checkout),
    }
    built.system = cli.System(
        env, http, run, checkout, tmp_path / "words", NOW, "ex-lab1"
    )
    return built


def secrets_absent(*texts: str) -> None:
    for text in texts:
        for secret in (
            OP_TOKEN,
            GH_TOKEN,
            OP_URL,
            "op-fake.test",
            SUFFIX,
            TAILNET_NAME,
        ):
            assert secret not in text
        assert "[[rules]]" not in text
        assert "regex" not in text


def test_a_dry_run_that_finds_no_item_says_it_would_create_it_and_writes_nothing(
    world: World,
) -> None:
    code, out, err = world.run("--dry-run")
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == (
        "dry run: op:\x2f/host-0-shared/scanner-policy/config would be created; wrote nothing"
    )
    assert {method for method, _ in world.connect.calls} == {"GET"}
    assert world.connect.bodies == []
    secrets_absent(out, err)


def test_a_run_creates_the_item_with_the_config_in_a_concealed_field(
    world: World,
) -> None:
    code, out, err = world.run()
    assert (code, err) == (0, "")
    assert (
        out.splitlines()[-1] == "created: op:\x2f/host-0-shared/scanner-policy/config"
    )
    ((method, path),) = [c for c in world.connect.calls if c[0] == "POST"]
    assert (method, path) == ("POST", "/v1/vaults/v-all/items")
    (item,) = world.connect.bodies
    (written,) = item["fields"]  # type: ignore[misc]
    assert written["label"] == "config"
    assert written["type"] == "CONCEALED"
    assert gitleaks.problem(written["value"]) is None
    assert r"secret\\-thing" in written["value"]
    secrets_absent(out, err)


def test_a_run_updates_an_item_whose_config_differs(world: World) -> None:
    world.connect.item = {
        "id": ITEM_ID,
        "fields": [
            {"label": "config", "value": "old"},
            {"label": "other", "value": "x"},
        ],
    }
    code, out, err = world.run()
    assert (code, err) == (0, "")
    assert (
        out.splitlines()[-1] == "updated: op:\x2f/host-0-shared/scanner-policy/config"
    )
    assert ("PUT", f"/v1/vaults/v-all/items/{ITEM_ID}") in world.connect.calls
    secrets_absent(out, err)


def test_a_dry_run_says_what_an_update_would_do_and_does_not_send_it(
    world: World,
) -> None:
    world.connect.item = {"id": ITEM_ID, "fields": []}
    code, out, _ = world.run("--dry-run")
    assert code == 0
    assert out.splitlines()[-1] == (
        "dry run: op:\x2f/host-0-shared/scanner-policy/config would be updated; wrote nothing"
    )
    assert "PUT" not in {method for method, _ in world.connect.calls}


def test_a_run_whose_item_is_current_changes_nothing(world: World) -> None:
    world.run()
    text = world.connect.bodies[0]["fields"][0]["value"]  # type: ignore[index]
    world.connect.item = {"id": ITEM_ID, "fields": [{"label": "config", "value": text}]}
    world.connect.calls.clear()
    code, out, _ = world.run()
    assert code == 0
    assert (
        out.splitlines()[-1] == "current: op:\x2f/host-0-shared/scanner-policy/config"
    )
    assert {method for method, _ in world.connect.calls} == {"GET"}
    code, out, _ = world.run("--dry-run")
    assert out.splitlines()[-1] == (
        "dry run: op:\x2f/host-0-shared/scanner-policy/config is current; wrote nothing"
    )


SUMMARY = """\
rules: 31, with the canary tbhb-private-canary present
  private-repo: 4 rule(s), 2 ordinary name(s), matched only as owner/name
  tailnet-suffix: 1 rule(s)
  tailnet-name: 1 rule(s)
  tailnet-host: 4 rule(s)
  tailnet-stem: 1 rule(s)
  tailnet-ipv4: 1 rule(s)
  tailnet-ipv6: 1 rule(s)
  home-path: 2 rule(s)
  checkout-path: 1 rule(s)
  tool-dir: 1 rule(s)
  private-ipv4: 3 rule(s)
  local-name: 1 rule(s)
  mac-address: 1 rule(s)
  op-reference: 1 rule(s)
  session-socket: 1 rule(s)
  account-name: 0 rule(s), 1 too common or short to match alone
  op-account: 1 rule(s)
  op-vault: 1 rule(s), 1 too common or short to match alone
  op-item: 1 rule(s)
  endpoint: 1 rule(s)
  keychain: 2 rule(s), 1 too common or short to match alone
repositories (4): ex-org/ab, ex-org/config, ex-org/dots, ex-org/secret-thing
hosts (4): ex-edge, ex-lab1, ex-lab2, ex-lab3
"""


def test_a_dry_run_prints_the_summary_and_never_the_config(world: World) -> None:
    _, out, _ = world.run("--dry-run")
    assert out == SUMMARY + (
        "dry run: op:\x2f/host-0-shared/scanner-policy/config would be created; "
        "wrote nothing\n"
    )


def test_the_hosts_come_from_the_inventory_and_not_from_tailscale(
    world: World,
) -> None:
    world.run("--dry-run")
    assert world.tailscale_calls == [["/bin/tailscale", "status", "--json"]]
    assert len(world.github_calls) == 1


def test_the_facts_come_from_each_source(world: World) -> None:
    creds = config.credentials(world.system.env)
    settings = config.settings(world.system.env)
    facts = cli._facts(world.system, creds, settings, {"b": "1", "a": "2", "": "3"})
    assert facts.repos == (
        ("ex-org", "secret-thing"),
        ("ex-org", "config"),
        ("ex-org", "ab"),
        ("ex-org", "dots"),
    )
    assert facts.tailnet == gitleaks.Tailnet(
        SUFFIX, TAILNET_NAME, ("ex-edge", "ex-lab1", "ex-lab2", "ex-lab3")
    )
    assert facts.words == {"alpha", "common", *gitleaks.TECH_WORDS}
    assert facts.operators == ("opr",)
    assert facts.checkout == settings.workspace
    assert facts.vaults == ("a", "b")
    assert facts.items == ("scanner-policy",)
    assert facts.endpoints == (f"op-connect.{SUFFIX}",)
    assert facts.names == ("ops-connect", "ops-connect.keychain", "srv")
    assert facts.account == "acct.example.com"
    assert facts.session_sockets == ("/tmp/sock/a.sock",)  # never opened


def test_a_retired_host_joins_the_hosts_the_rules_and_the_stems_are_made_from(
    world: World,
) -> None:
    retired = world.system.cwd / "ops" / "ansible" / "inventory" / "retired-hosts.yml"
    write(retired, "retired_hosts: [Ex-Lab9, ex-gone]\n")
    creds = config.credentials(world.system.env)
    settings = config.settings(world.system.env)
    facts = cli._facts(world.system, creds, settings, {})
    assert facts.tailnet.hosts == (
        "ex-edge",
        "ex-gone",
        "ex-lab1",
        "ex-lab2",
        "ex-lab3",
        "ex-lab9",
    )
    made, _ = gitleaks.rules(facts)
    assert sum(rule.category == "tailnet-host" for rule in made) == 6
    assert any("ex\\-gone" in rule.regex for rule in made)
    assert gitleaks.stems(facts.tailnet.hosts) == ["ex-lab"]


def test_a_server_host_is_lowercased_under_the_suffix_and_listed_once(
    world: World,
) -> None:
    creds = config.credentials(world.system.env)
    settings = replace(
        config.settings(world.system.env), server_hosts=("Op-A", "op-a", "op-b")
    )
    facts = cli._facts(world.system, creds, settings, {})
    assert facts.endpoints == (f"op-a.{SUFFIX}", f"op-b.{SUFFIX}")


def test_the_summary_says_when_the_canary_is_missing(world: World) -> None:
    creds = config.credentials(world.system.env)
    settings = config.settings(world.system.env)
    facts = cli._facts(world.system, creds, settings, {"host-0-shared": "x"})
    made, omitted = gitleaks.rules(facts)
    lines = cli.summary("[extend]", made, omitted, facts)
    assert (
        lines[0]
        == f"rules: {len(made) + 1}, with the canary tbhb-private-canary missing"
    )
    assert lines[1] == (
        "  private-repo: 4 rule(s), 2 ordinary name(s), matched only as owner/name"
    )
    assert lines[-2].startswith("repositories (4): ")
    assert lines[-1] == "hosts (4): ex-edge, ex-lab1, ex-lab2, ex-lab3"


def test_a_category_that_left_nothing_out_says_nothing_about_it() -> None:
    facts = gitleaks.Facts(
        (),
        gitleaks.Tailnet("s", "n", ()),
        frozenset(),
        (),
        None,
        (),
        (),
        (),
        (),
        "a",
        (),
    )
    rule = gitleaks.Rule("op-item", "x")
    lines = cli.summary("", [rule], {"op-item": 0, "endpoint": 3}, facts)
    assert "  op-item: 1 rule(s)" in lines
    assert "  endpoint: 0 rule(s), 3 too common or short to match alone" in lines
    assert lines[0] == "rules: 2, with the canary tbhb-private-canary missing"


def test_missing_credentials_stop_the_run_before_it_reaches_anything(
    world: World,
) -> None:
    env = {k: v for k, v in world.system.env.items() if k != config.GITHUB_TOKEN}
    world.system = replace(world.system, env=env)
    code, out, err = world.run()
    assert (code, out) == (1, "")
    assert err == (
        "tbhbops gitleaks generate: missing from the environment: "
        "TBHBOPS_GITHUB_TOKEN (a read-only GitHub token that lists the private "
        "repositories). @tbhb runs this command under their own session until "
        "they exist; wrote nothing\n"
    )
    assert world.connect.calls == []
    assert world.github_calls == []
    secrets_absent(err)


def test_both_missing_credentials_are_named_and_neither_value_is_printed(
    world: World,
) -> None:
    env = {"XDG_CONFIG_HOME": world.system.env["XDG_CONFIG_HOME"]}
    world.system = replace(world.system, env=env)
    code, _, err = world.run("--dry-run")
    assert code == 1
    assert "OP_CONNECT_TOKEN (a 1Password Connect token with write access" in err
    assert "TBHBOPS_GITHUB_TOKEN (a read-only GitHub token" in err
    assert "OP_CONNECT_HOST (the Connect server's https URL)" in err


def test_a_problem_with_the_config_is_reported_after_the_summary_and_writes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:

    def wrong(_text: str, _ref: gitleaks.ItemRef = gitleaks.DEFAULT_REF) -> str:
        return "the config is wrong"

    monkeypatch.setattr(gitleaks, "problem", wrong)
    code, out, err = world.run()
    assert code == 1
    assert out == SUMMARY
    assert err == "tbhbops gitleaks generate: the config is wrong; wrote nothing\n"
    assert world.connect.bodies == []
    assert "POST" not in {method for method, _ in world.connect.calls}


def test_a_token_that_does_not_reach_the_all_vault_stops_the_run(
    world: World,
) -> None:
    world.connect.vaults = {"ab": "v2"}
    code, out, err = world.run()
    assert code == 1
    assert out == SUMMARY.replace("rules: 31", "rules: 30").replace(
        "op-vault: 1 rule(s), 1 too", "op-vault: 0 rule(s), 1 too"
    )
    assert err == (
        "tbhbops gitleaks generate: the Connect token doesn't reach the vault "
        "host-0-shared; "
        "wrote nothing\n"
    )
    assert world.connect.calls == [("GET", "/v1/vaults")]


def test_a_failing_source_is_reported_without_its_details(world: World) -> None:
    def failing(_argv: list[str]) -> str:
        msg = "tailscale status failed: exit 1"
        raise CliError(msg)

    world.system = replace(world.system, run=failing)
    code, out, err = world.run()
    assert (code, out) == (1, "")
    assert (
        err
        == "tbhbops gitleaks generate: tailscale status failed: exit 1; wrote nothing\n"
    )


def test_help_names_the_environment_and_never_a_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(["gitleaks", "generate", "--help"])
    assert raised.value.code == 0
    shown = capsys.readouterr().out
    for name, use in config.ENVIRONMENT:
        assert f"  {name}\n      {use}\n" in shown
    assert "--dry-run" in shown
    assert "@tbhb runs this command under their own session" in shown
    assert "ansible/inventory/" in shown
    assert "never includes the config" in shown
    assert "op:\x2f/host-0-shared/scanner-policy/config" not in shown
    assert "config field of the 1Password item scanner-policy in the" in shown
    assert "host-0-shared" in shown


def test_the_command_needs_both_words_and_takes_only_dry_run() -> None:
    args = cli.parser().parse_args(["gitleaks", "generate"])
    assert (args.group, args.command, args.dry_run) == ("gitleaks", "generate", False)
    assert cli.parser().parse_args(["gitleaks", "generate", "--dry-run"]).dry_run
    bad: list[list[str]] = [[], ["gitleaks"], ["gitleaks", "generate", "--write"]]
    for argv in bad:
        with pytest.raises(SystemExit):
            cli.parser().parse_args(argv)


def test_the_real_system_is_this_process() -> None:
    system = cli.real_system()
    assert system.env is os.environ
    assert system.http is https_request
    assert system.run is sources.run_command
    assert system.cwd == Path.cwd()
    assert system.words == sources.WORDS


def test_main_runs_the_command_on_the_real_system_and_returns_its_status(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "real_system", lambda: world.system)
    assert cli.main(["gitleaks", "generate", "--dry-run"]) == 0
    assert capsys.readouterr().out == SUMMARY + (
        "dry run: op:\x2f/host-0-shared/scanner-policy/config would be created; "
        "wrote nothing\n"
    )
    monkeypatch.setattr(cli, "real_system", lambda: replace(world.system, env={}))
    assert cli.main(["gitleaks", "generate"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(
        "tbhbops gitleaks generate: missing from the environment"
    )


def test_main_reads_the_arguments_of_the_process_when_given_none(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "real_system", lambda: world.system)
    monkeypatch.setattr(
        cli.sys, "argv", ["tbhbops", "gitleaks", "generate", "--dry-run"]
    )
    assert cli.main() == 0
    assert "dry run:" in capsys.readouterr().out


def help_of(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> str:
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        cli.parser().parse_args([*argv, "--help"])
    return capsys.readouterr().out


def test_every_level_of_help_says_what_it_is_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    top = help_of(monkeypatch, capsys)
    assert top.startswith("usage: tbhbops ")
    assert "\nThe tbhb-dev/tbhbdev tools.\n" in top
    assert re.search(r"\n +gitleaks +the private gitleaks config\n", top)
    leaks = help_of(monkeypatch, capsys, "gitleaks")
    assert leaks.startswith("usage: tbhbops gitleaks ")
    assert re.search(
        r"\n +generate +generate the private gitleaks config and write it to "
        r"1Password\n",
        leaks,
    )
    generate = help_of(monkeypatch, capsys, "gitleaks", "generate")
    assert generate.startswith("usage: tbhbops gitleaks generate ")
    assert (
        "\nGenerate the private gitleaks config and write it to the config field of "
        "the 1Password item scanner-policy in the host-0-shared vault.\n"
    ) in generate
    assert re.search(
        r"\n +--dry-run +print what it would write, which never includes the config, "
        r"and stop\n",
        generate,
    )


def test_dns_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert re.search(
        r"\n +dns +the CoreDNS network resolver\n", help_of(monkeypatch, capsys)
    )
    assert help_of(monkeypatch, capsys, "dns") == (
        "usage: tbhbops dns [-h] {smoke} ...\n\n"
        "positional arguments:\n  {smoke}\n"
        "    smoke     start CoreDNS in Docker and check its views and forwarder\n\n"
        "options:\n  -h, --help  show this help message and exit\n"
    )
    assert help_of(monkeypatch, capsys, "dns", "smoke") == (
        "usage: tbhbops dns smoke [-h] [--source SOURCE] [--artifacts ARTIFACTS]\n\n"
        "Start the pinned CoreDNS image with the project's Corefile, zones and "
        "hardening flags inside Docker, and check the LAN view, tailnet view, "
        "refused view and forwarder block.\n\n"
        "options:\n  -h, --help            show this help message and exit\n"
        "  --source SOURCE       the DNS Compose project directory; default apps/dns\n"
        "  --artifacts ARTIFACTS\n"
        "                        the directory for failure logs; default "
        "dns-smoke-artifacts\n"
    )
    with pytest.raises(SystemExit) as missing_command:
        cli.parser().parse_args(["dns"])
    assert missing_command.value.code == 2


def configure_item_ref(world: World, ref: str) -> None:
    cfg = Path(world.system.env["XDG_CONFIG_HOME"]) / "tbhbdev" / "config.toml"
    text = cfg.read_text(encoding="utf-8")
    cfg.write_text(
        text.replace("[gitleaks]\n", f'[gitleaks]\nitem_ref = "{ref}"\n'),
        encoding="utf-8",
    )


def test_generate_uses_a_configured_item_reference(world: World) -> None:
    ref = "op:\x2f/vault-a/item-a/field-a"
    configure_item_ref(world, ref)
    world.connect.vaults = {"vault-a": "v-custom", "ab": "v2"}

    creds = config.credentials(world.system.env)
    settings = config.settings(world.system.env)
    facts = cli._facts(world.system, creds, settings, world.connect.vaults)
    assert facts.items == ("item-a",)

    code, out, err = world.run()
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == f"created: {ref}"
    assert ("POST", "/v1/vaults/v-custom/items") in world.connect.calls
    assert (
        "GET",
        '/v1/vaults/v-custom/items?filter=title eq "item-a"',
    ) in world.connect.calls
    (item,) = world.connect.bodies
    assert item["title"] == "item-a"
    (written,) = item["fields"]  # type: ignore[misc]
    assert written["id"] == "field-a"
    assert written["label"] == "field-a"
    assert r"item\\-a" in written["value"]
    secrets_absent(out, err)


def test_a_token_that_does_not_reach_the_configured_vault_is_refused(
    world: World,
) -> None:
    configure_item_ref(world, "op:\x2f/vault-a/item-a/field-a")
    world.connect.vaults = {"host-0-shared": "v-all", "ab": "v2"}
    code, _, err = world.run()
    assert code == 1
    assert err == (
        "tbhbops gitleaks generate: the Connect token doesn't reach the vault "
        "vault-a; wrote nothing\n"
    )
    assert world.connect.calls == [("GET", "/v1/vaults")]


def test_dry_run_out_writes_config_with_mode_0600_and_publishes_nothing(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_chmod(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("chmod must not be called")

    monkeypatch.setattr(os, "chmod", fail_chmod)
    target = tmp_path / "preview.toml"
    code, out, err = world.run("--dry-run", "--out", str(target))
    assert (code, err) == (0, "")
    assert out == SUMMARY + f"dry run: wrote config to {target}\n"
    secrets_absent(out, err)
    assert world.connect.calls == [("GET", "/v1/vaults")]
    assert world.connect.bodies == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    written = target.read_text(encoding="utf-8")
    assert gitleaks.problem(written) is None
    assert world.run()[0] == 0
    assert written == world.connect.bodies[0]["fields"][0]["value"]  # type: ignore[index]


def test_out_errors_and_help(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "preview.toml"
    code, out, err = world.run("--out", str(target))
    assert (code, out) == (1, "")
    assert "--dry-run" in err
    assert not target.exists()
    assert world.connect.calls == []

    existing = tmp_path / "existing.toml"
    existing.write_text("keep-me\n", encoding="utf-8")
    code, _, err = world.run("--dry-run", "--out", str(existing))
    assert code == 1
    assert str(existing) in err
    assert existing.read_text(encoding="utf-8") == "keep-me\n"
    assert world.connect.bodies == []

    missing = tmp_path / "missing-dir" / "preview.toml"
    code, _, err = world.run("--dry-run", "--out", str(missing))
    assert code == 1
    assert err == f"tbhbops gitleaks generate: cannot write {missing}; wrote nothing\n"

    shown = help_of(monkeypatch, capsys, "gitleaks", "generate")
    assert "--out" in shown
    assert "[gitleaks] item_ref" in shown
