"""Stage fixtures for command tests with scripted backends."""

import pytest
from tbhbops_cli.o11y.smoke import (
    alerts,
    capacity,
    exports,
    grafana,
    lifecycle,
    monitoring,
    telemetry,
)


def skip(*_args: object) -> None:
    pass


@pytest.fixture
def before_monitoring(monkeypatch: pytest.MonkeyPatch) -> None:
    for module, names in (
        (capacity, ("smoke", "cleanup")),
        (monitoring, ("group7",)),
        (lifecycle, ("group8", "group9", "group10", "group13")),
        (telemetry, ("group11",)),
        (alerts, ("group12",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, skip)


@pytest.fixture
def early_stages(monkeypatch: pytest.MonkeyPatch, before_monitoring: None) -> None:
    monkeypatch.setattr(grafana, "group4", skip)
    for name in ("group5", "export_persistence", "group6"):
        monkeypatch.setattr(exports, name, skip)
