"""Tests for the Ansible inventory reader."""

from pathlib import Path

import pytest
from tbhbops_cli import inventory
from tbhbops_cli.common import CliError

HOSTS = """\
all:
  children:
    lab:
      hosts:
        Lab-1:
        lab-2:
    macs:
      hosts:
        mac-1:
      children:
        deeper:
          hosts:
            deep-1:
        empty:
          hosts:
    edge:
      hosts:
        edge-1:
    again:
      hosts:
        lab-2:
"""


def make(root: Path, hosts: str = HOSTS) -> Path:
    directory = root / "ansible" / "inventory"
    directory.mkdir(parents=True)
    (directory / "hosts.yml").write_text(hosts, encoding="utf-8")
    return directory


def var_file(directory: Path, kind: str, name: str, text: str) -> None:
    (directory / kind).mkdir(exist_ok=True)
    (directory / kind / f"{name}.yml").write_text(text, encoding="utf-8")


def test_the_inventory_is_found_from_a_directory_below_the_checkout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "monorepo"
    ansible = root / "ops" / "ansible"
    ansible.mkdir(parents=True)
    (ansible / "ansible.cfg").touch()
    directory = make(tmp_path / "tbhbdev-private" / "ops")
    below = root / "ops" / "packages" / "x"
    below.mkdir(parents=True)
    assert inventory.find(below, {}) == directory
    assert inventory.find(root, {}) == directory


def test_an_explicit_companion_root_is_used(tmp_path: Path) -> None:
    companion = tmp_path / "custom"
    directory = make(companion / "ops")
    assert inventory.find(tmp_path, {"TBHB_PRIVATE_ROOT": str(companion)}) == directory


def test_no_inventory_is_an_error_naming_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(CliError, match=r"no ansible/inventory/hosts\.yml in") as raised:
        inventory.find(tmp_path)
    assert "TBHB_PRIVATE_ROOT" in str(raised.value)


def test_a_named_inventory_directory_is_taken_as_it_is(tmp_path: Path) -> None:
    directory = make(tmp_path)
    assert inventory.at(directory) == directory


def test_a_named_directory_without_hosts_yml_is_an_error_naming_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(CliError, match=r"^no hosts\.yml in ") as raised:
        inventory.at(tmp_path)
    assert str(raised.value).endswith(str(tmp_path))


def test_every_host_is_listed_once_lowercase_and_sorted(tmp_path: Path) -> None:
    directory = make(tmp_path)
    var_file(directory, "host_vars", "edge-1", "ansible_host: Ex-Edge\nother: 1\n")
    assert inventory.hosts(directory) == (
        "deep-1",
        "ex-edge",
        "lab-1",
        "lab-2",
        "mac-1",
    )


def test_a_host_var_file_that_is_not_a_mapping_leaves_the_name_alone(
    tmp_path: Path,
) -> None:
    directory = make(tmp_path)
    var_file(directory, "host_vars", "edge-1", "- ansible_host\n")
    assert "edge-1" in inventory.hosts(directory)


@pytest.mark.parametrize(
    "alias",
    ["198.51.100.153", "host.example.net", "9lives", "", "has space", "[1]", "3", "-x"],
)
def test_a_name_that_is_not_short_is_refused_by_the_inventory_name(
    tmp_path: Path, alias: str
) -> None:
    directory = make(tmp_path)
    var_file(directory, "host_vars", "edge-1", f"ansible_host: '{alias}'\n")
    with pytest.raises(CliError) as raised:
        inventory.hosts(directory)
    assert str(raised.value) == "host edge-1 has no short tailnet name in the inventory"


def test_an_ansible_host_that_is_not_text_is_refused(tmp_path: Path) -> None:
    directory = make(tmp_path)
    var_file(directory, "host_vars", "edge-1", "ansible_host:\n  - a\n")
    with pytest.raises(CliError, match="host edge-1 has no short"):
        inventory.hosts(directory)


@pytest.mark.parametrize("text", ["all:\n", "{}\n", "- all\n", "all:\n  hosts:\n"])
def test_an_inventory_without_hosts_is_an_error(tmp_path: Path, text: str) -> None:
    directory = make(tmp_path, text)
    with pytest.raises(CliError, match="lists no hosts") as raised:
        inventory.hosts(directory)
    assert str(raised.value).startswith(str(directory / "hosts.yml"))


def test_one_letter_host_names_are_short_names(tmp_path: Path) -> None:
    directory = make(tmp_path, "all:\n  hosts:\n    a:\n    b-1:\n")
    assert inventory.hosts(directory) == ("a", "b-1")


@pytest.mark.parametrize("text", ["a: [", "a: 'unterminated", "\tx: y"])
def test_yaml_that_wont_parse_is_refused_without_quoting_it(
    tmp_path: Path, text: str
) -> None:
    directory = make(tmp_path, text)
    with pytest.raises(CliError) as raised:
        inventory.hosts(directory)
    assert str(raised.value) == "cannot read hosts.yml as YAML"
    assert raised.value.__suppress_context__


def test_a_host_vars_file_that_wont_parse_is_named_by_file(tmp_path: Path) -> None:
    directory = make(tmp_path)
    var_file(directory, "host_vars", "edge-1", "a: [\n")
    with pytest.raises(CliError, match=r"cannot read edge-1\.yml as YAML"):
        inventory.hosts(directory)


def test_the_operators_come_from_group_vars_and_host_vars(tmp_path: Path) -> None:
    directory = make(tmp_path)
    var_file(directory, "group_vars", "agent_hosts", "agent_account_operator: zed\n")
    var_file(directory, "group_vars", "lab", "agent_account_operator: abe\n")
    var_file(directory, "host_vars", "lab-1", "agent_account_operator: zed\n")
    var_file(directory, "group_vars", "all", "agent_account_operator: ''\n")
    var_file(directory, "group_vars", "macs", "agent_account_operator: 3\n")
    var_file(directory, "group_vars", "none", "other: x\n")
    assert inventory.operators(directory) == ("abe", "zed")


def test_one_kind_of_vars_directory_may_be_missing(tmp_path: Path) -> None:
    directory = make(tmp_path)
    var_file(directory, "host_vars", "lab-1", "agent_account_operator: abe\n")
    assert inventory.operators(directory) == ("abe",)


def test_an_inventory_without_an_operator_is_an_error(tmp_path: Path) -> None:
    directory = make(tmp_path)
    with pytest.raises(CliError) as raised:
        inventory.operators(directory)
    assert str(raised.value) == "the inventory sets no agent_account_operator"


def test_a_group_lists_the_hosts_below_it_by_inventory_name_sorted(
    tmp_path: Path,
) -> None:
    directory = make(tmp_path)
    var_file(directory, "host_vars", "edge-1", "ansible_host: ex-edge\n")
    assert inventory.group(directory, "macs") == ("deep-1", "mac-1")
    assert inventory.group(directory, "deeper") == ("deep-1",)
    assert inventory.group(directory, "edge") == ("edge-1",)


def test_a_group_found_only_in_a_later_branch_is_found(tmp_path: Path) -> None:
    text = (
        "all:\n  children:\n    first:\n      hosts:\n        a-1:\n"
        "    second:\n      children:\n        wanted:\n          hosts:\n"
        "            b-1:\n"
    )
    assert inventory.group(make(tmp_path, text), "wanted") == ("b-1",)


@pytest.mark.parametrize("name", ["nope", "empty"])
def test_a_group_that_is_missing_or_empty_is_an_error(
    tmp_path: Path, name: str
) -> None:
    directory = make(tmp_path)
    with pytest.raises(CliError) as raised:
        inventory.group(directory, name)
    assert str(raised.value) == (
        f"{directory / 'hosts.yml'} has no group {name} with hosts"
    )


def test_a_group_in_a_hosts_file_that_is_not_yaml_is_refused_by_file_name(
    tmp_path: Path,
) -> None:
    directory = make(tmp_path, "all: [unclosed\n")
    with pytest.raises(CliError) as raised:
        inventory.group(directory, "g")
    assert str(raised.value) == "cannot read hosts.yml as YAML"


def test_a_group_host_that_is_not_a_short_name_is_refused(tmp_path: Path) -> None:
    text = "all:\n  children:\n    g:\n      hosts:\n        198.51.100.153:\n"
    with pytest.raises(CliError) as raised:
        inventory.group(make(tmp_path, text), "g")
    assert str(raised.value) == "host 198.51.100.153 in group g isn't a short name"


def test_a_group_named_with_no_body_takes_the_hosts_defined_elsewhere(
    tmp_path: Path,
) -> None:
    text = (
        "all:\n  children:\n    lab:\n      hosts:\n        a-1:\n"
        "    mac:\n      hosts:\n        b-1:\n"
        "    both:\n      hosts:\n        c-1:\n      children:\n        lab:\n"
        "          hosts:\n            a-2:\n        mac:\n"
    )
    assert inventory.group(make(tmp_path, text), "both") == ("a-1", "a-2", "b-1", "c-1")


def test_a_group_that_contains_itself_is_listed_once_and_ends(tmp_path: Path) -> None:
    text = (
        "all:\n  children:\n    a:\n      hosts:\n        a-1:\n      children:\n"
        "        b:\n    b:\n      hosts:\n        b-1:\n      children:\n        a:\n"
    )
    assert inventory.group(make(tmp_path, text), "a") == ("a-1", "b-1")


def retired(directory: Path, text: str) -> None:
    (directory / "retired-hosts.yml").write_text(text, encoding="utf-8")


def test_retired_hosts_are_lowercased_sorted_and_listed_once(tmp_path: Path) -> None:
    directory = make(tmp_path)
    retired(directory, "retired_hosts:\n  - Zed-1\n  - abc-2\n  - ABC-2\n")
    assert inventory.retired_hosts(directory) == ("abc-2", "zed-1")


@pytest.mark.parametrize("text", ["retired_hosts: []\n", "{}\n", "retired_hosts:\n"])
def test_a_list_with_no_retired_hosts_is_allowed(tmp_path: Path, text: str) -> None:
    directory = make(tmp_path)
    retired(directory, text)
    assert inventory.retired_hosts(directory) == ()


@pytest.mark.parametrize("name", ["198.51.100.153", "a.b", "-a", "''", "3"])
def test_a_retired_host_that_is_not_a_short_name_is_refused(
    tmp_path: Path, name: str
) -> None:
    directory = make(tmp_path)
    retired(directory, f"retired_hosts: [{name}]\n")
    with pytest.raises(CliError) as raised:
        inventory.retired_hosts(directory)
    assert str(raised.value) == (
        "retired-hosts.yml has a retired host with no short tailnet name"
    )


def test_a_missing_retired_hosts_file_is_an_error(tmp_path: Path) -> None:
    directory = make(tmp_path)
    with pytest.raises(CliError) as raised:
        inventory.retired_hosts(directory)
    assert str(raised.value) == (
        f"{directory / 'retired-hosts.yml'} is missing; "
        "it lists the retired hosts, maybe none"
    )


def test_a_retired_hosts_file_that_is_not_yaml_is_an_error(tmp_path: Path) -> None:
    directory = make(tmp_path)
    retired(directory, "a: [")
    with pytest.raises(CliError) as raised:
        inventory.retired_hosts(directory)
    assert str(raised.value) == "cannot read retired-hosts.yml as YAML"
