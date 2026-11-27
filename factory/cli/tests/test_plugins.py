"""Tests for config files from entry points."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import TYPE_CHECKING

import pytest

from tbhbdev import plugins

if TYPE_CHECKING:
    from conftest import Installed


def test_the_real_lookup_reads_the_plugin_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups: list[str] = []

    def entry_points(*, group: str) -> list[EntryPoint]:
        groups.append(group)
        return []

    monkeypatch.setattr(plugins, "entry_points", entry_points)
    plugins.load.cache_clear()
    assert plugins.load() == ()
    assert groups == ["tbhbdev.plugins"]


def test_plugins_load_once(installed: Installed) -> None:
    installed.stub("one")
    first = plugins.load()
    installed.points.clear()
    assert plugins.load() is first


def test_a_plugin_that_fails_to_import_is_reported_and_skipped(
    installed: Installed, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = "tbhbdev_no_such_plugin_module"
    installed.points.append(EntryPoint("broken", missing, plugins.GROUP))
    installed.stub("good")
    installed.add("raises", RuntimeError("boom"))
    assert [name for name, _ in plugins.load()] == ["good"]
    assert capsys.readouterr().err == (
        "tbhbdev: cannot load plugin 'broken': "
        f"ModuleNotFoundError: No module named {missing!r}\n"
        "tbhbdev: cannot load plugin 'raises': RuntimeError: boom\n"
    )


def test_config_uses_the_plugin_that_supplies_the_file(
    installed: Installed,
) -> None:
    installed.add("bare", object())
    installed.stub("other", files={"other.toml": "x"})
    installed.stub("one", files={"secrets.toml": "[connect]\n"})
    assert plugins.supplied("secrets.toml") == "[connect]\n"


def test_config_two_plugins_supply_is_an_error(installed: Installed) -> None:
    installed.stub("one", files={"secrets.toml": "a"})
    installed.stub("two", files={"secrets.toml": "b"})
    with pytest.raises(
        plugins.PluginError, match=r"plugins 'one', 'two' each supply secrets\.toml"
    ):
        plugins.supplied("secrets.toml")
