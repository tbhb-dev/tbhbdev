"""Check telemetry play coverage against the public inventory group structure."""

from pathlib import Path

import pytest
import yaml
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader

PLAYBOOKS = next(
    path / "ops" / "ansible" / "playbooks"
    for path in Path(__file__).parents
    if (path / "ops" / "ansible").is_dir()
)

GAP_GROUPS = {
    "lab_macos": "No macOS telemetry role or macOS Molecule scenario.",
    "operator_macs": "No macOS telemetry role or macOS Molecule scenario.",
    "windows": "No Windows telemetry role.",
}


@pytest.fixture
def inventory(tmp_path: Path) -> InventoryManager:
    source = tmp_path / "inventory.yml"
    source.write_text(
        """\
agent_hosts:
  children:
    linux_agent_hosts:
      hosts:
        linux-a:
      children:
        lab:
          hosts:
            lab-a:
    lab_macos:
      hosts:
        lab-mac-a:
    operator_macs:
      hosts:
        operator-mac-a:
    windows:
      hosts:
        windows-a:
""",
        encoding="utf-8",
    )
    return InventoryManager(loader=DataLoader(), sources=[str(source)])


@pytest.fixture
def telemetry_hosts(inventory: InventoryManager) -> set[str]:
    covered = set()
    for playbook in sorted(PLAYBOOKS.glob("*.yml")):
        plays = yaml.safe_load(playbook.read_text(encoding="utf-8")) or []
        for play in plays:
            roles = [
                role.get("role") if isinstance(role, dict) else role
                for role in play.get("roles", [])
            ]
            if "host_telemetry" in roles:
                covered.update(host.name for host in inventory.get_hosts(play["hosts"]))
    return covered


def test_every_agent_host_has_a_telemetry_play(
    inventory: InventoryManager, telemetry_hosts: set[str]
) -> None:
    expected = {host.name for host in inventory.get_hosts("agent_hosts")}
    for group in GAP_GROUPS:
        expected.difference_update(host.name for host in inventory.get_hosts(group))

    missing = expected - telemetry_hosts
    assert not missing, f"No telemetry play for {sorted(missing)}"


def test_gap_groups_are_still_uncovered(
    inventory: InventoryManager, telemetry_hosts: set[str]
) -> None:
    agent_groups = {
        group.name for group in inventory.groups["agent_hosts"].get_descendants()
    }
    for group in GAP_GROUPS:
        assert group in agent_groups, f"{group} is no longer under agent_hosts"
        assert telemetry_hosts.isdisjoint(
            host.name for host in inventory.get_hosts(group)
        ), f"{group} now has telemetry coverage"
