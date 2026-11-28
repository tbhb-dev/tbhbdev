"""Install pinned versions through mise and expose them on the account's PATH."""

from typing import TYPE_CHECKING

import pytest
from fake_tool import write_fake

from tbhbdev.alpha.install import check_tools, command

if TYPE_CHECKING:
    from unit.alpha.install.conftest import World


def test_command_leaves_matching_check_tools_alone(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.repo / "mise.toml").write_text('[tools]\nnode = "1.2.3"\n')
    directory = world.home / ".local" / "bin"
    directory.mkdir(parents=True)
    node = directory / "node"
    write_fake(node, "print('v1.2.3')\n")
    before = node.stat().st_mtime_ns
    write_fake(world.bin / "mise", "raise SystemExit('unexpected mise call')\n")
    assert command.main([], "p") == 0
    assert "pinned check tool" not in capsys.readouterr().out
    assert node.stat().st_mtime_ns == before
    assert not node.is_symlink()


def test_repair_installs_pins_and_links_versions(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.repo / "mise.toml").write_text(
        '[tools]\nnode = "1.2.3"\n"aqua:boyter/scc" = "4.5.6"\n'
    )
    node, scc = world.bin / "new-node", world.bin / "new-scc"
    write_fake(node, "print('v1.2.3')\n")
    write_fake(scc, "print('scc version 4.5.6')\n")
    (world.bin / "node").symlink_to(node)
    write_fake(
        world.bin / "mise",
        "import sys\n"
        "match sys.argv[1:]:\n"
        "    case ['install', 'node@1.2.3', 'aqua:boyter/scc@4.5.6']: pass\n"
        f"    case ['which', 'node', '--tool=node@1.2.3']: print({str(node)!r})\n"
        "    case ['which', 'scc', '--tool=aqua:boyter/scc@4.5.6']:\n"
        f"        print({str(scc)!r})\n"
        "    case _: sys.exit(1)\n",
    )
    directory = world.home / ".local" / "bin"
    assert check_tools.install_check_tools(dry_run=True) == 0
    assert "would install and link pinned check tools" in capsys.readouterr().out
    assert not directory.exists()
    assert check_tools.install_check_tools(dry_run=False) == 0
    assert (directory / "node").readlink() == node
    assert (directory / "scc").readlink() == scc


@pytest.mark.parametrize("body", ["raise SystemExit(1)", "print('not absolute')"])
def test_mise_failure_leaves_tool_links_alone(
    world: World, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    (world.repo / "mise.toml").write_text('[tools]\nnode = "1.2.3"\n')
    write_fake(world.bin / "mise", body)
    assert check_tools.install_check_tools(dry_run=False) == 1
    assert "could not install pinned check tools" in capsys.readouterr().err
    assert not (world.home / ".local" / "bin" / "node").exists()
