"""Render the runner pool's egress table and check the Connect exception.

With connect, the table accepts TCP port 443 to the Connect host's addresses
ahead of its drops. With service_account, it has no such rule.
"""

from pathlib import Path
from typing import cast

import jinja2
import pytest
import yaml

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "roles").is_dir()
)
ROLE = ANSIBLE / "roles" / "actions_runner_pool"
V4 = "192.0.2.1"
V6 = "2001:db8::1"


def _render(**extra: object) -> list[str]:
    environment = jinja2.Environment(  # noqa: S701 - renders nftables text, not HTML
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
    )
    variables = cast(
        "dict[str, object]",
        yaml.safe_load((ROLE / "vars" / "main.yml").read_text(encoding="utf-8")),
    )
    variables |= {"ansible_managed": "managed", "actions_runner_pool_uid": 1000}
    template = environment.get_template("actions-runner-egress.nft.j2")
    rendered = template.render(**variables, **extra)
    return [line.strip() for line in rendered.splitlines()]


def _chain(lines: list[str]) -> list[str]:
    start = lines.index("chain service_user {")
    body = lines[start + 1 : lines.index("}", start)]
    return [line for line in body if line and not line.startswith("#")]


def test_connect_accepts_port_443_to_the_resolved_addresses_before_the_drops() -> None:
    chain = _chain(
        _render(
            actions_runner_pool_op_auth="connect",
            actions_runner_pool_connect_v4=[V4, "192.0.2.1"],
            actions_runner_pool_connect_v6=[V6],
        )
    )
    v4 = f"ip daddr {{ {V4}, 192.0.2.1 }} tcp dport 443 accept"
    v6 = f"ip6 daddr {{ {V6} }} tcp dport 443 accept"
    first_drop = next(i for i, line in enumerate(chain) if line.endswith("drop"))
    assert chain.index(v4) < first_drop
    assert chain.index(v6) < first_drop
    assert [line for line in chain if "dport 443" in line] == [v4, v6]


@pytest.mark.parametrize(
    ("v4", "v6", "family"),
    [([V4], [], "ip daddr"), ([], [V6], "ip6 daddr")],
)
def test_connect_emits_only_the_families_that_resolved(
    v4: list[str], v6: list[str], family: str
) -> None:
    chain = _chain(
        _render(
            actions_runner_pool_op_auth="connect",
            actions_runner_pool_connect_v4=v4,
            actions_runner_pool_connect_v6=v6,
        )
    )
    rules = [line for line in chain if "dport 443" in line]
    assert len(rules) == 1
    assert rules[0].startswith(f"{family} {{")


def test_service_account_has_no_port_443_rule() -> None:
    chain = _chain(_render(actions_runner_pool_op_auth="service_account"))
    assert not [line for line in chain if "dport 443" in line]
    assert chain[-1].startswith("ip6 daddr @blocked_v6")


def _block_tasks() -> list[dict[str, object]]:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    return [
        inner
        for task in tasks
        for inner in cast("list[dict[str, object]]", task.get("block", []))
    ]


def test_connect_token_lookup_skips_a_token_file_in_place() -> None:
    tasks = _block_tasks()
    names = [str(task["name"]) for task in tasks]
    look = names.index("Look for a Connect token already in place")
    install = names.index("Install the Connect token from the controller lookup")
    assert look < install
    stat = cast("dict[str, object]", tasks[look]["ansible.builtin.stat"])
    assert stat["path"] == (
        "{{ actions_runner_pool_home }}/{{ actions_runner_pool_op_token_file }}"
    )
    assert stat["get_checksum"] is False
    assert stat["get_mime"] is False
    assert tasks[look]["check_mode"] is False
    assert tasks[look]["register"] == "actions_runner_pool_token_in_place"
    skip = (
        "not (actions_runner_pool_token_in_place.stat.exists and "
        "actions_runner_pool_token_in_place.stat.size > 0)"
    )
    assert skip in cast("list[str]", tasks[install]["when"])
