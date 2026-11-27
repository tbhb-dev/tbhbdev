"""Render guest firewall rules and evaluate the libvirt drift guard locally."""

from pathlib import Path
from typing import cast

import jinja2
import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "roles").is_dir()
)
ROLE = ANSIBLE / "roles" / "host_hardening"


def _defaults() -> dict[str, object]:
    return cast(
        "dict[str, object]",
        yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")),
    )


def _rules(variables: dict[str, object]) -> list[str]:
    environment = jinja2.Environment(  # noqa: S701 - nftables text, not HTML
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
    )
    rendered = environment.get_template("host.nft.j2").render(
        {"host_hardening_lab_subnet": "192.0.2.0/24"} | variables,
        ansible_managed="Local test",
    )
    return [line.strip() for line in rendered.splitlines() if "iifname {" in line]


def test_default_guest_rules_allow_only_the_default_and_lab_bridges() -> None:
    assert _defaults()["host_hardening_libvirt_bridges"] == ["virbr0"]
    assert _rules(_defaults()) == [
        'iifname { "virbr0", "br-lab" } udp dport { 53, 67 } accept',
        'iifname { "virbr0", "br-lab" } tcp dport 53 accept',
    ]


@pytest.mark.parametrize("bridge", ["virbr-o11y", "virbr-metrics"])
def test_inventory_bridge_is_shared_by_the_vm_and_both_guest_rules(bridge: str) -> None:
    loader = DataLoader()
    variables = _defaults() | {
        "o11y_bridge": bridge,
        "o11y_vm_network_bridge": trust_as_template("{{ o11y_bridge }}"),
        "host_hardening_libvirt_bridges": trust_as_template(
            "{{ ['virbr0', o11y_bridge] }}"
        ),
    }
    templar = Templar(loader=loader, variables=variables)
    assert templar.template(variables["o11y_vm_network_bridge"]) == bridge
    variables["host_hardening_libvirt_bridges"] = templar.template(
        variables["host_hardening_libvirt_bridges"]
    )
    assert _rules(variables) == [
        f'iifname {{ "virbr0", "{bridge}", "br-lab" }} udp dport {{ 53, 67 }} accept',
        f'iifname {{ "virbr0", "{bridge}", "br-lab" }} tcp dport 53 accept',
    ]


@pytest.mark.parametrize("check_mode", [False, True])
@pytest.mark.parametrize(
    ("network", "bridge", "bridges", "allowed"),
    [
        ("o11y", "virbr-o11y", ["virbr0"], False),
        ("o11y", "virbr-o11y", ["virbr0", "virbr-o11y"], True),
        ("lab", "br-lab", ["virbr0"], True),
    ],
)
def test_drift_assert_requires_the_discovered_bridge(
    network: str, bridge: str, bridges: list[str], allowed: bool, check_mode: bool
) -> None:
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks" / "libvirt.yml"), trusted_as_template=True
    )
    guard = tasks[2]["block"][2]
    variables = {
        "ansible_check_mode": check_mode,
        "host_hardening_libvirt_bridges": bridges,
        "host_hardening_lab_bridge": _defaults()["host_hardening_lab_bridge"],
        "item": {
            "item": network,
            "stdout_lines": [f"Name:           {network}", f"Bridge:         {bridge}"],
        },
    } | guard["vars"]
    templar = Templar(loader=loader, variables=variables)
    assert templar.evaluate_expression(guard["when"]) is True
    assert (
        templar.evaluate_expression(guard["ansible.builtin.assert"]["that"]) is allowed
    )
    message = templar.template(guard["ansible.builtin.assert"]["fail_msg"])
    assert bridge in message
    assert network in message
    assert "host_hardening_libvirt_bridges" in message
    assert "host_hardening_lab_bridge" in message


@pytest.mark.parametrize("check_mode", [False, True])
def test_drift_assert_skips_networks_without_a_bridge(check_mode: bool) -> None:
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks" / "libvirt.yml"), trusted_as_template=True
    )
    guard = tasks[2]["block"][2]
    variables = {
        "ansible_check_mode": check_mode,
        "item": {
            "item": "direct",
            "stdout_lines": ["Name:           direct", "Active:         yes"],
        },
    } | guard["vars"]
    templar = Templar(loader=loader, variables=variables)
    assert templar.evaluate_expression(guard["when"]) is False


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("virbr0", True),
        ("virbr-o11y", True),
        ("br_1.2", True),
        ('bad"bridge', False),
        ("virbr0\n", False),
        ("", False),
    ],
)
def test_bridge_names_are_validated(name: str, allowed: bool) -> None:
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks" / "libvirt.yml"), trusted_as_template=True
    )
    templar = Templar(loader=loader, variables={"item": name})
    assert (
        templar.evaluate_expression(tasks[0]["ansible.builtin.assert"]["that"])
        is allowed
    )


def test_discovery_runs_in_check_mode_and_skips_hosts_without_virsh() -> None:
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks" / "libvirt.yml"), trusted_as_template=True
    )
    probe, block = tasks[1:]
    assert probe["ansible.builtin.command"] == {"cmd": "virsh --version"}
    assert probe["failed_when"] is False
    for task in [probe, *block["block"][:2]]:
        assert task["check_mode"] is False
        assert task["changed_when"] is False
    for rc in [0, 1]:
        templar = Templar(loader=loader, variables={"host_hardening_virsh": {"rc": rc}})
        assert templar.evaluate_expression(block["when"]) is (rc == 0)
    assert block["environment"] == {"LC_ALL": "C"}
    discover, info, guard = block["block"]
    assert discover["ansible.builtin.command"]["argv"] == [
        "virsh",
        "--connect=qemu:///system",
        "net-list",
        "--all",
        "--name",
    ]
    assert info["ansible.builtin.command"]["argv"] == [
        "virsh",
        "--connect=qemu:///system",
        "net-info",
        "{{ item }}",
    ]
    templar = Templar(
        loader=loader,
        variables={
            "host_hardening_networks": {"stdout_lines": ["default", "", "o11y"]}
        },
    )
    assert templar.template(info["loop"]) == ["default", "o11y"]
    assert guard["loop"] == "{{ host_hardening_network_info.results }}"
    main = loader.load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert main[0]["ansible.builtin.import_tasks"] == "libvirt.yml"
    assert "ansible.builtin.template" in main[3]
    specs = loader.load_from_file(str(ROLE / "meta" / "argument_specs.yml"))
    option = specs["argument_specs"]["main"]["options"][
        "host_hardening_libvirt_bridges"
    ]
    assert option["type"] == "list"
    assert option["elements"] == "str"
    assert option["default"] == ["virbr0"]
