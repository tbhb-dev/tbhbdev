"""Exercise event gateway deployment conditions without targeting a host."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).resolve().parents
    if (path / "ansible" / "roles").is_dir()
)
ROLE = ANSIBLE / "roles" / "event_gateway_deploy"
LOADER = DataLoader()

MAIN_TASKS = LOADER.load_from_file(
    str(ROLE / "tasks/main.yml"), trusted_as_template=True
)
CONTAINER_TASKS = LOADER.load_from_file(
    str(ROLE / "tasks/containers.yml"), trusted_as_template=True
)
START = CONTAINER_TASKS[-2]
GATEWAY_ID = "sha256:" + "a" * 64
TUNNEL_ID = "sha256:" + "b" * 64


def test_playbook_targets_the_companion_group() -> None:
    plays = yaml.safe_load((ANSIBLE / "playbooks" / "event-gateway.yml").read_text())
    assert plays[0]["hosts"] == "event_gateway"


@pytest.mark.parametrize(
    ("commit", "accepted"),
    [
        ("0123456789abcdefABCD" * 2, True),
        ("f31b3ec2", False),
        ("g" * 40, False),
        ("a" * 40 + "\n", False),
    ],
)
def test_pin_requires_full_git_hash(commit: str, accepted: bool) -> None:
    templar = Templar(loader=LOADER, variables={"event_gateway_deploy_commit": commit})
    assert (
        templar.evaluate_expression(MAIN_TASKS[0]["ansible.builtin.assert"]["that"])
        is accepted
    )


def test_secret_preflight_reads_only_file_metadata() -> None:
    check = MAIN_TASKS[2]
    assert check["loop"] == ["github_webhook_secret", "tunnel_token"]
    assert check["ansible.builtin.stat"] == {
        "path": "{{ event_gateway_deploy_checkout }}/deploy/secrets/{{ item }}",
        "get_checksum": False,
        "get_attributes": False,
        "get_mime": False,
    }
    failure = MAIN_TASKS[3]["ansible.builtin.assert"]
    templar = Templar(
        loader=LOADER,
        variables={
            "item": {"item": "tunnel_token", "stat": {"exists": False}},
            "event_gateway_deploy_checkout": "/home/ops/event-gateway-deploy/repo",
        },
    )
    assert templar.evaluate_expression(failure["that"]) is False
    assert templar.template(failure["fail_msg"]).endswith(
        "/home/ops/event-gateway-deploy/repo/deploy/secrets/tunnel_token"
    )


@pytest.mark.parametrize("inspect_status", [0, 1])
def test_build_requires_a_missing_tag(inspect_status: int) -> None:
    templar = Templar(
        loader=LOADER, variables={"event_gateway_deploy_tag": {"rc": inspect_status}}
    )
    assert templar.evaluate_expression(CONTAINER_TASKS[1]["when"]) is bool(
        inspect_status
    )


@pytest.mark.parametrize(
    ("gateway", "tunnel", "start"),
    [
        ("", "", True),
        (f"{GATEWAY_ID} true", f"{TUNNEL_ID} true", False),
        (f"{TUNNEL_ID} true", f"{TUNNEL_ID} true", True),
        (f"{GATEWAY_ID} true", f"{GATEWAY_ID} true", True),
        (f"{GATEWAY_ID} true", f"{TUNNEL_ID} false", True),
    ],
)
def test_start_uses_local_image_id_only_when_running_images_differ(
    gateway: str, tunnel: str, start: bool
) -> None:
    templar = Templar(
        loader=LOADER,
        variables={
            **START["vars"],
            "event_gateway_deploy_gateway_image": {"stdout": GATEWAY_ID},
            "event_gateway_deploy_tunnel_image": {"stdout": TUNNEL_ID},
            "event_gateway_deploy_running": {
                "results": [{"stdout": gateway}, {"stdout": tunnel}]
            },
        },
    )
    assert templar.evaluate_expression(START["when"]) is start
    assert templar.template(START["ansible.builtin.command"]["argv"]) == [
        "deploy/up.sh",
        GATEWAY_ID,
        "--pull",
        "never",
    ]
    assert START["environment"] == {"DEPLOY_DOCKER_SUDO": "1"}


@pytest.mark.parametrize(
    ("status", "output", "healthy"),
    [(0, "true healthy", True), (0, "true starting", False), (1, "", False)],
)
def test_health_probe_requires_a_running_healthy_container(
    status: int, output: str, healthy: bool
) -> None:
    task = CONTAINER_TASKS[-1]
    templar = Templar(
        loader=LOADER,
        variables={
            "event_gateway_deploy_health": SimpleNamespace(rc=status, stdout=output)
        },
    )
    assert task["loop"] == ["gateway", "tunnel"]
    assert templar.evaluate_expression(task["until"]) is healthy
