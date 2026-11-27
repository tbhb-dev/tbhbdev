"""Simulated Docker and Compose commands for deployment tests."""

import json
from dataclasses import replace
from pathlib import Path
from typing import override

import pytest
from tbhbops.deploy.command import Command, CommandError
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.protocol import Json
lazy from tbhbops.deploy.config import Service

from .deploy_model import minimal

RELEASE = "a" * 40


class FakeCommand(Command):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.budgets: list[dict[str, object]] = []
        self.model: Json = minimal()
        self.fixed: Json = None
        self.inspected: Json = []
        self.names = b""
        self.resources: Json = []
        self.active = False
        self.stopped_units: set[str] = set()
        self.failed = False
        self.fail_argv: list[str] = []
        self.info = json.dumps(
            {
                "Driver": "overlayfs",
                "ServerVersion": "29.9.3",
                "PidsLimit": True,
                "MemoryLimit": True,
                "SwapLimit": True,
            }
        ).encode()
        self.info_error: Exception | None = None

    @override
    def __call__(
        self, argv: list[str], data: bytes | None = None, **kwargs: object
    ) -> bytes:
        self.calls.append((argv, data))
        self.budgets.append(kwargs)
        if self.failed or argv == self.fail_argv:
            raise CommandError
        if argv[:2] == ["/usr/bin/docker", "info"]:
            return self.daemon_info()
        if argv[0] == "/usr/bin/systemctl":
            return self.systemctl(argv)
        if "config" in argv:
            value = (
                self.fixed
                if self.fixed is not None
                and "--env-file" in argv
                and argv[argv.index("--env-file") + 1] == "/dev/null"
                else self.model
            )
            return json.dumps(value).encode()
        if argv[:2] == ["/usr/bin/docker", "inspect"]:
            return json.dumps(self.inspected).encode()
        if "ls" in argv or "inspect" in argv:
            return self.names if "ls" in argv else json.dumps(self.resources).encode()
        return (
            b"abc"
            if argv[:3] == ["/usr/bin/docker", "ps", "-aq"] and self.inspected
            else b""
        )

    def daemon_info(self) -> bytes:
        if self.info_error is not None:
            raise self.info_error
        return self.info

    def systemctl(self, argv: list[str]) -> bytes:
        if argv[1] == "stop":
            self.stopped_units.add(argv[-1])
        return (
            b"active\n"
            if self.active and argv[-1] not in self.stopped_units
            else b"inactive\n"
        )


@pytest.fixture
def engine(deploy_service: Service, trusted_tree: Path) -> Engine:
    secret = replace(
        deploy_service.secrets[0], target=trusted_tree / "data" / "secrets" / "token"
    )
    service = replace(
        deploy_service,
        root=trusted_tree,
        secrets=(secret,),
        ro_binds=(
            trusted_tree / "data" / "web" / "config",
            trusted_tree / "data" / "secrets" / "generated",
        ),
    )
    for path in (
        trusted_tree / "deploy",
        trusted_tree / "deploy" / "models",
        trusted_tree / "releases",
        trusted_tree / "releases" / RELEASE,
    ):
        path.mkdir(mode=0o700)
    return Engine(service, "/compose", FakeCommand())
