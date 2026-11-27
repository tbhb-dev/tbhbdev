"""Simulated release and daemon fixtures for receiver tests."""

import io
from dataclasses import replace
from typing import override
from unittest.mock import patch
lazy from pathlib import Path

import pytest
from tbhbops.deploy import compose, receiver_io, staging
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.command import CommandError
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.protocol import (
    Json,
    Request,
    decode,
    mapping,
    pack,
)
from tbhbops.deploy.receiver import Receiver
lazy from tbhbops.deploy.config import Service

from .deploy_fakes import FakeCommand
from .deploy_model import RELEASE, minimal

PREVIOUS = "b" * 40


class ReceiveEngine(Engine):
    def __init__(self, service: Service) -> None:
        super().__init__(service, "/compose", FakeCommand())
        self.events: list[str] = []
        self.fail_at: set[str] = set()

    def event(self, name: str) -> None:
        self.events.append(name)
        if name in self.fail_at:
            raise CommandError(name)

    @override
    def parse(self, release: str) -> None:
        self.event("parse")
        fs.write(self.model_path(release), b"{}")

    @override
    def stored(self, release: str) -> Json:
        return {}

    @override
    def stop(self) -> None:
        self.event("stop")

    @override
    def stop_unit(self) -> None:
        self.event("stop-unit")

    @override
    def check_paths(
        self, candidate: Json, release: str, *, missing: bool = False, live: bool = True
    ) -> None:
        self.event("paths " + release)

    @override
    def run(self, release: str, *arguments: str, timeout_s: float = 300) -> None:
        self.event(arguments[0] + " " + release)
        if "--force-recreate" in arguments:
            self.events.append("force-recreate")

    @override
    def local_images(self, release: str) -> bool:
        return True

    @override
    def health(self, release: str) -> None:
        self.event("health " + release)


@pytest.fixture
def receiver(
    deploy_service: Service, trusted_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> Receiver:
    root = trusted_tree
    target = root / "data" / "secrets" / "token"
    target.parent.mkdir(parents=True, mode=0o700)
    service = replace(
        deploy_service,
        root=root,
        init=(),
        secrets=(replace(deploy_service.secrets[0], target=target),),
        min_free_gib=0,
    )
    wanted = Request("o11y", RELEASE, "123")
    body = pack(
        wanted,
        {"release/compose.yaml": b"model", "release/config/web": b"config"},
        {"token": b"fixture"},
    )

    def read_stream(_limit: int) -> bytes:
        return body

    monkeypatch.setattr(receiver_io, "stream", read_stream)
    return Receiver(wanted, service, ReceiveEngine(service), io.StringIO())


def engine(receiver: Receiver) -> ReceiveEngine:
    assert isinstance(receiver.engine, ReceiveEngine)
    return receiver.engine


def result(receiver: Receiver) -> dict[str, Json]:
    assert isinstance(receiver.out, io.StringIO)
    return mapping(decode(receiver.out.getvalue().splitlines()[-1].encode()))


def align_secret_group(receiver: Receiver) -> None:
    secret = receiver.service.secrets[0]
    receiver.service = replace(
        receiver.service,
        secrets=(replace(secret, gid=secret.target.parent.stat().st_gid),),
    )
    receiver.engine.service = receiver.service


def seed_previous(receiver: Receiver) -> None:
    staging.prepare(receiver.service)
    (receiver.service.root / "releases" / PREVIOUS).mkdir()
    fs.pointer(receiver.service.root / "current", "releases/" + PREVIOUS)
    fs.write(receiver.service.secrets[0].target, b"earlier")


class RetryCommand(FakeCommand):
    """Model the daemon state changed by Compose and read by real health checks."""

    def __init__(self) -> None:
        super().__init__()
        self.running = {"web": False, "worker": False}
        self.fail_up = False
        self.fail_health = False
        self.model = {
            "name": "o11y",
            "services": {
                name: mapping(minimal()["services"])["web"] for name in self.running
            },
        }

    @override
    def __call__(
        self, argv: list[str], data: bytes | None = None, **kwargs: object
    ) -> bytes:
        self.inspected = [
            {
                "Config": {"Labels": {"com.docker.compose.service": name}},
                "State": {"Running": running},
                "Mounts": [],
            }
            for name, running in self.running.items()
        ]
        output = super().__call__(argv, data, **kwargs)
        if "up" in argv:
            if self.fail_up:
                raise CommandError
            self.running = dict.fromkeys(self.running, not self.fail_health)
        return output


@pytest.fixture
def retry_receiver(receiver: Receiver, monkeypatch: pytest.MonkeyPatch) -> Receiver:
    secret = receiver.service.secrets[0]
    receiver.service = replace(
        receiver.service,
        secrets=(replace(secret, gid=secret.target.parent.stat().st_gid),),
        timeout_s=1,
    )
    receiver.engine = Engine(receiver.service, "/compose", RetryCommand())
    use_health_clock(receiver.engine, monkeypatch)
    seed_previous(receiver)
    assert receiver.run() == 0
    receiver.staging.created = False
    return receiver


def use_health_clock(daemon: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance simulated daemon failures without permitting retries past the budget."""
    original = daemon.health

    def health(release: str) -> None:
        now = 0.0

        def advance(seconds: float) -> None:
            nonlocal now
            assert now < daemon.service.timeout_s
            assert 0 < seconds <= daemon.service.timeout_s - now
            now += seconds

        with (
            patch.object(compose.time, "monotonic", side_effect=lambda: now),
            patch.object(compose.time, "sleep", side_effect=advance),
        ):
            original(release)

    monkeypatch.setattr(daemon, "health", health)
