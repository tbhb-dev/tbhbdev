"""Render the Connect endpoint expansion's expressions with Ansible's templar.

The tests read the expressions out of the shared task file, so they judge the
file's text, not a copy of it.
"""

import json
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "roles").is_dir()
)
SUFFIX = "example-tailnet.example.invalid"
STATUS = json.dumps({"BackendState": "Running", "MagicDNSSuffix": SUFFIX})

_TASKS = cast(
    "list[dict[str, object]]",
    yaml.safe_load(
        (ANSIBLE / "tasks" / "connect-endpoints.yml").read_text(encoding="utf-8")
    ),
)
_EXPAND = next(task for task in _TASKS if "block" in task)
_BLOCK = cast("list[dict[str, dict[str, object]]]", _EXPAND["block"])
_WHEN = cast("str", _EXPAND["when"])
_ARGV = cast("list[str]", _BLOCK[0]["ansible.builtin.command"]["argv"])
_APPEND = cast(
    "str", _BLOCK[2]["ansible.builtin.set_fact"]["connect_endpoints_resolved"]
)


def _render(expression: str, **variables: object) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return cast("object", templar.template(trust_as_template(expression)))


def _expands(endpoints: list[str]) -> bool:
    return bool(_render("{{ " + _WHEN + " }}", connect_endpoints=endpoints))


def _resolve(endpoints: list[str]) -> list[str]:
    return cast(
        "list[str]",
        _render(
            _APPEND,
            connect_endpoints=endpoints,
            connect_endpoints_status={"stdout": STATUS},
        ),
    )


@pytest.mark.parametrize(
    ("endpoints", "expected"),
    [
        (["https://op-connect"], ["https://op-connect.example-tailnet.example.invalid"]),
        (
            ["https://op-connect:8443"],
            ["https://op-connect.example-tailnet.example.invalid:8443"],
        ),
        (["https://op-connect.other.example.invalid"], ["https://op-connect.other.example.invalid"]),
        (
            ["https://op-connect.other.example.invalid:8443"],
            ["https://op-connect.other.example.invalid:8443"],
        ),
        (["https://op-connect/path"], ["https://op-connect/path"]),
        (["https://op-connect:8443/path"], ["https://op-connect:8443/path"]),
        (["https://user@op-connect"], ["https://user@op-connect"]),
        (["https://op-connect:"], ["https://op-connect:"]),
        (
            [
                "https://op-connect",
                "https://op-connect:8443",
                "https://op-connect.other.example.invalid",
                "https://op-connect/path",
            ],
            [
                "https://op-connect.example-tailnet.example.invalid",
                "https://op-connect.example-tailnet.example.invalid:8443",
                "https://op-connect.other.example.invalid",
                "https://op-connect/path",
            ],
        ),
    ],
)
def test_short_names_gain_the_suffix_before_any_port_and_others_pass_through(
    endpoints: list[str], expected: list[str]
) -> None:
    assert _resolve(endpoints) == expected


@pytest.mark.parametrize(
    ("endpoints", "expected"),
    [
        (["https://op-connect"], True),
        (["https://op-connect:8443"], True),
        (["https://op-connect.other.example.invalid"], False),
        (["https://op-connect/path"], False),
        (["https://user@op-connect"], False),
        (["https://op-connect.other.example.invalid", "https://op-connect:8443"], True),
        (["https://op-connect.other.example.invalid", "https://op-connect/path"], False),
    ],
)
def test_the_lookup_runs_only_when_a_short_name_is_present(
    endpoints: list[str], *, expected: bool
) -> None:
    assert _expands(endpoints) is expected


def test_the_binary_defaults_to_tailscale() -> None:
    assert _render(_ARGV[0]) == "tailscale"
    assert _ARGV[1:] == ["status", "--json"]


def test_the_binary_variable_overrides_the_default() -> None:
    assert (
        _render(_ARGV[0], connect_endpoints_tailscale="/opt/tailscale")
        == "/opt/tailscale"
    )
