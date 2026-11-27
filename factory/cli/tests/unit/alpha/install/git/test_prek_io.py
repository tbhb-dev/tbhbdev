"""Tests for install prek."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from unit.alpha.install.conftest import World


import pytest
from fake_tool import write_fake

from tbhbdev.alpha.install import model
from tbhbdev.alpha.install.git import prek


def test_find_prek(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert prek.find_prek() == ["prek"]
    only_mise = tmp_path / "only_mise"
    only_mise.mkdir()
    write_fake(only_mise / "mise", "")
    monkeypatch.setenv("PATH", str(only_mise))
    assert prek.find_prek() == ["mise", "exec", "--", "prek"]
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    with pytest.raises(model.InstallError, match=r"^prek not found; install it"):
        prek.find_prek()
    assert world.prek_calls() == []


def test_prek_install_passes_output_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    prek.prek_install(["prek"], world.repo, "--hook-type", "pre-push")
    assert capsys.readouterr() == ("prek installed pre-push\n", "")
    assert world.prek_calls() == [["install", "--hook-type", "pre-push"]]
    (world.bin / "prek.fail").write_text("")
    with pytest.raises(
        model.InstallError, match=r"^prek install --hook-type x failed$"
    ):
        prek.prek_install(["prek"], world.repo, "--hook-type", "x")
    assert capsys.readouterr() == ("", "prek: boom\n")
