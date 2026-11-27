"""Tests for Connect token selection, names, and grants."""

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Self, override

import pytest
from tbhbops_cli import token_policy
from tbhbops_cli.common import CliError

AGENTS = ("ex-ctl1", "ex-ctl2", "ex-lab1", "ex-win1")
CONTROLLERS = ("ex-ctl1", "ex-ctl2")
WHEN = "20261108T142100Z"
CI_VMS = ("ex-lab1-ci", "ex-ctl1-ci")


def test_an_ordinary_host_reads_its_own_vault_and_host_all() -> None:
    assert token_policy.grants("ex-lab1", AGENTS, CONTROLLERS) == (
        token_policy.Grant("host-ex-lab1", "r"),
        token_policy.Grant("host-0-shared", "r"),
    )


def test_a_controller_writes_host_all_and_reads_every_agent_vault() -> None:
    assert token_policy.grants("ex-ctl2", AGENTS, CONTROLLERS) == (
        token_policy.Grant("host-0-shared", ""),
        *(token_policy.Grant(f"host-{h}", "r") for h in AGENTS),
    )


@pytest.mark.parametrize(
    ("permission", "option"),
    [("", "--vault v"), ("r", "--vault v,r"), ("w", "--vault v,w")],
)
def test_a_vault_option_adds_at_most_one_suffix(permission: str, option: str) -> None:
    assert token_policy.vault_option(token_policy.Grant("v", permission)) == option


def test_a_token_name_of_the_limit_passes() -> None:
    host = "h" * (token_policy.NAME_LIMIT - len("agent-host::20261108T142100Z"))
    name = token_policy.token_name("agent-host", host, "20261108T142100Z")
    assert len(name) == token_policy.NAME_LIMIT == 64


def test_a_token_name_over_the_limit_stops_the_script() -> None:
    host = "h" * (token_policy.NAME_LIMIT + 1 - len("agent-host::20261108T142100Z"))
    with pytest.raises(CliError) as caught:
        token_policy.token_name("agent-host", host, "20261108T142100Z")
    assert str(caught.value) == (
        f"the token name agent-host:{host}:20261108T142100Z has 65 characters,"
        " over the 64 Connect accepts"
    )


def test_the_ops_controller_grants_are_read_only() -> None:
    assert token_policy.ops_grants() == (
        token_policy.Grant("host-storage-nas-a", "r"),
        token_policy.Grant("host-storage-nas-b", "r"),
    )
    assert (
        token_policy.ops_controller("ex-ctl1", CONTROLLERS) == token_policy.ops_grants()
    )


def test_an_ops_controller_must_be_an_operator_mac() -> None:
    with pytest.raises(CliError) as caught:
        token_policy.ops_controller("ex-lab1", CONTROLLERS)
    assert str(caught.value) == "not an operator Mac in the inventory: ex-lab1"


def test_the_ops_hosts_are_the_selected_operator_macs_in_name_order() -> None:
    assert token_policy.select_ops(("b", "a", "c"), ("c", "x", "a")) == ("a", "c")
    assert token_policy.select_ops(CONTROLLERS, ("ex-lab1",)) == ()


@pytest.mark.parametrize("host", ["storage-nas-a", "storage-nas-b"])
def test_a_host_ops_vault_in_a_grant_is_refused(host: str) -> None:
    with pytest.raises(CliError, match=rf"refusing to grant host-{host}: no token"):
        token_policy.grants(host, (*AGENTS, host), CONTROLLERS)
    with pytest.raises(CliError, match="host-storage-"):
        token_policy.grants("ex-ctl1", (*AGENTS, host), CONTROLLERS)


def test_the_stamp_is_in_utc() -> None:
    local = datetime(2026, 11, 8, 9, 21, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert token_policy.stamp(local) == WHEN
    assert token_policy.stamp(datetime(2026, 11, 8, 14, 21, 0, tzinfo=UTC)) == WHEN


class _AsUtcOnly(datetime):
    @override
    def astimezone(self, tz: tzinfo | None = None) -> Self:
        assert tz is UTC
        return super().astimezone(tz)


def test_the_stamp_converts_to_utc_not_the_system_zone() -> None:
    now = _AsUtcOnly(2026, 11, 8, 9, 21, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert token_policy.stamp(now) == WHEN


def test_select_takes_every_agent_host_unless_narrowed() -> None:
    assert token_policy.select(AGENTS, []) == AGENTS
    assert token_policy.select(AGENTS, ["ex-win1", "ex-lab1", "ex-win1"]) == (
        "ex-lab1",
        "ex-win1",
    )


def test_select_refuses_a_host_that_is_not_an_agent_host() -> None:
    with pytest.raises(CliError) as caught:
        token_policy.select(AGENTS, ["ex-lab1", "zz", "aa"])
    assert str(caught.value) == "not an agent host in the inventory: aa, zz"


def test_reach_marks_each_vault_ok_missing_or_extra_in_name_order() -> None:
    assert token_policy.reach({"b", "c", "x"}, {"a", "b", "c"}) == [
        ("missing", "a"),
        ("ok", "b"),
        ("ok", "c"),
        ("extra", "x"),
    ]
    assert token_policy.reach(set(), set()) == []


def test_the_ops_session_grants_are_an_operator_macs_and_never_a_host_ops_vault() -> (
    None
):
    found = token_policy.session_grants("ex-ctl2", AGENTS, CONTROLLERS)
    assert found == (
        *token_policy.grants("ex-ctl2", AGENTS, CONTROLLERS),
        token_policy.Grant("overlay-oauth", token_policy.READ),
    )
    assert found[0] == token_policy.Grant("host-0-shared", token_policy.READ_WRITE)
    assert {g.vault for g in found[1:]} == {
        *(f"host-{a}" for a in AGENTS),
        "overlay-oauth",
    }
    assert all(g.permission == token_policy.READ for g in found[1:])
    assert not any(g.vault.startswith(token_policy.FORBIDDEN) for g in found)


def test_the_ops_session_host_must_be_an_operator_mac() -> None:
    with pytest.raises(CliError) as caught:
        token_policy.session_grants("ex-lab1", AGENTS, CONTROLLERS)
    assert str(caught.value) == "not an operator Mac in the inventory: ex-lab1"


def test_the_lab_node_of_a_runner_vm_is_its_name_without_the_suffix() -> None:
    assert token_policy.node_of("host-a-ci") == "host-a"
    assert token_policy.node_of("host-a") == "host-a"


def test_select_runners_takes_the_named_vms_in_name_order() -> None:
    assert token_policy.select_runners(
        CI_VMS, AGENTS, ["ex-lab1-ci", "ex-ctl1-ci"]
    ) == (
        "ex-ctl1-ci",
        "ex-lab1-ci",
    )


def test_select_runners_refuses_a_vm_that_is_not_in_the_ci_group() -> None:
    with pytest.raises(CliError) as caught:
        token_policy.select_runners(
            CI_VMS, AGENTS, ["ex-lab1-ci", "ex-zzz-ci", "ex-aaa-ci"]
        )
    assert str(caught.value) == (
        "not a CI runner VM in the inventory: ex-aaa-ci, ex-zzz-ci"
    )


@pytest.mark.parametrize("vm", ["ex-lab1", "ex-gone-ci"])
def test_select_runners_refuses_a_vm_whose_node_is_no_agent_host(vm: str) -> None:
    with pytest.raises(CliError) as caught:
        token_policy.select_runners((*CI_VMS, vm), AGENTS, [vm])
    assert str(caught.value) == (
        f"the lab node of {vm} is not an agent host in the inventory:"
        " its token has no vault to go in"
    )
