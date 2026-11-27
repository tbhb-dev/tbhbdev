"""Test Docker capability and ownership checks."""

import json
from unittest.mock import call, patch

import pytest
from tbhbops.deploy import daemon
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.protocol import Json, RefusalError

from ...deploy_fakes import FakeCommand


def test_capabilities_names_the_service_requiring_the_limit(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    info = json.loads(fake.info)
    info["SwapLimit"] = False
    fake.info = json.dumps(info).encode()
    candidate: Json = {
        "services": {
            "web": {"pids_limit": 10, "mem_limit": 1024},
            "worker": {"memswap_limit": 2048},
        }
    }
    with pytest.raises(
        RefusalError, match=r"^daemon cannot enforce SwapLimit for service worker$"
    ):
        daemon.capabilities(engine.command, candidate)


def test_capabilities_requires_only_limits_present_in_services(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fake.info = (
        b'{"Driver":"overlayfs","ServerVersion":"29.9.3",'
        b'"PidsLimit":false,"MemoryLimit":false,"SwapLimit":false}'
    )
    daemon.capabilities(engine.command, {"services": {"web": {}}})


def test_resource_ownership(engine: Engine) -> None:
    candidate: Json = {
        "networks": {"default": {"name": "o11y_default"}},
        "volumes": {"cache": {"name": "o11y_cache"}},
    }
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    daemon.ownership(engine.command, engine.service.name, candidate)
    fake.names = b"o11y_default\no11y_cache\n"
    fake.resources = [{"Labels": {"com.docker.compose.project": "o11y"}}]
    daemon.ownership(engine.command, engine.service.name, candidate)
    fake.resources = [{"Labels": {"com.docker.compose.project": "foreign"}}]
    with pytest.raises(RefusalError, match=r"^foreign resource ownership$"):
        daemon.ownership(engine.command, engine.service.name, candidate)
    fake.resources = []
    with pytest.raises(RefusalError, match=r"^resource inspect type$"):
        daemon.ownership(engine.command, engine.service.name, candidate)


def test_resource_ownership_exact_queries(engine: Engine) -> None:
    candidate: Json = {
        "networks": {"a": {"name": "o11y_a"}},
        "volumes": {"b": {"name": "o11y_b"}},
    }
    with patch.object(
        engine,
        "command",
        side_effect=[
            b"o11y_a\n",
            b'[{"Labels":{"com.docker.compose.project":"o11y"}}]',
            b"o11y_b\n",
            b'[{"Labels":{"com.docker.compose.project":"o11y"}}]',
        ],
    ) as command:
        daemon.ownership(engine.command, engine.service.name, candidate)
    assert command.call_args_list == [
        call(["/usr/bin/docker", "network", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "network", "inspect", "o11y_a"]),
        call(["/usr/bin/docker", "volume", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "volume", "inspect", "o11y_b"]),
    ]


def test_resource_listing_continues_after_an_absent_resource(engine: Engine) -> None:
    candidate: Json = {
        "networks": {
            "absent": {"name": "o11y_absent"},
            "present": {"name": "o11y_present"},
        }
    }
    with patch.object(
        engine,
        "command",
        side_effect=[
            b"o11y_present\n",
            b"o11y_present\n",
            b'[{"Labels":{"com.docker.compose.project":"o11y"}}]',
        ],
    ) as command:
        daemon.ownership(engine.command, engine.service.name, candidate)
    assert command.call_args_list == [
        call(["/usr/bin/docker", "network", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "network", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "network", "inspect", "o11y_present"]),
    ]


def test_resource_without_labels_is_foreign(engine: Engine) -> None:
    with (
        patch.object(engine, "command", side_effect=[b"o11y_a", b"[{}]"]),
        pytest.raises(RefusalError, match=r"^foreign resource ownership$"),
    ):
        daemon.ownership(
            engine.command, engine.service.name, {"networks": {"a": {"name": "o11y_a"}}}
        )
