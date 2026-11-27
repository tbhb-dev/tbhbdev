"""Tests for install shims."""

import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from unit.alpha.install.expectations import shim_path

from tbhbdev.alpha import gh_shim
from tbhbdev.alpha.install import scripts, shims

if TYPE_CHECKING:
    from unit.alpha.install.conftest import World


def test_install_gh_shim_writes_then_is_up_to_date(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    path = shim_path()
    assert shims.install_gh_shim(dry_run=False) == 0
    assert capsys.readouterr().out == f"gh-shim: wrote {path}\n"
    assert path.read_text() == gh_shim.wrapper_text()
    assert path.stat().st_mode & 0o7777 == 0o755
    assert shims.install_gh_shim(dry_run=False) == 0
    assert shims.install_gh_shim(dry_run=True) == 0
    assert capsys.readouterr().out == f"gh-shim: {path} is up to date\n" * 2
    assert [p.name for p in path.parent.iterdir()] == ["gh"]


def test_install_gh_shim_needs_no_tbhbdev_on_path(world: World) -> None:
    del world
    assert shutil.which("tbhbdev") is None
    assert shims.install_gh_shim(dry_run=False) == 0
    assert shlex.quote(sys.executable) in shim_path().read_text()


@pytest.mark.parametrize(("text", "mode"), [(None, 0o644), ("old\n", 0o755)])
def test_install_gh_shim_replaces_a_stale_shim(
    world: World, capsys: pytest.CaptureFixture[str], text: str | None, mode: int
) -> None:
    del world
    path = shim_path()
    path.parent.mkdir(parents=True)
    path.write_text(text or gh_shim.wrapper_text())
    path.chmod(mode)
    assert shims.install_gh_shim(dry_run=True) == 0
    assert capsys.readouterr().out == f"gh-shim: would write {path}\n"
    assert path.stat().st_mode & 0o777 == mode
    assert shims.install_gh_shim(dry_run=False) == 0
    assert capsys.readouterr().out == f"gh-shim: wrote {path}\n"
    assert path.read_text() == gh_shim.wrapper_text()
    assert path.stat().st_mode & 0o777 == 0o755


def test_install_gh_shim_reports_a_failure(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    (tmp_path / "xdg-data").write_text("")
    path = shim_path()
    assert shims.install_gh_shim(dry_run=False) == 1
    assert capsys.readouterr() == (
        "",
        (
            f"tbhbdev alpha install: gh-shim: cannot write {path}: [Errno 20] "
            f"Not a directory: {str(path.parent)!r}\n"
        ),
    )


def test_install_ssh_shim_writes_then_is_up_to_date(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    path = gh_shim.bin_dir() / "ssh"
    assert shims.install_ssh_shim(dry_run=False) == 0
    assert capsys.readouterr().out == f"ssh-shim: wrote {path}\n"
    assert path.read_text() == scripts.SSH_SHIM_TEXT
    assert path.stat().st_mode & 0o7777 == 0o755
    assert shims.install_ssh_shim(dry_run=True) == 0
    assert capsys.readouterr().out == f"ssh-shim: {path} is up to date\n"
    path.write_text(f"{scripts.SSH_SHIM_MARK}\nold\n")
    assert shims.install_ssh_shim(dry_run=True) == 0
    assert capsys.readouterr().out == f"ssh-shim: would write {path}\n"


def shim_ssh_options(
    tmp_path: Path, args: list[str], env: dict[str, str]
) -> dict[str, str]:
    """The `ssh -G` options in force through the installed shim, by name."""
    config = tmp_path / "ssh_config"
    config.write_text(f"Host *\n  IdentityAgent {tmp_path}/agent.sock\n")
    done = subprocess.run(
        [str(gh_shim.bin_dir() / "ssh"), *args, "-F", str(config), "-G", "h.invalid"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", **env},
    )
    options = (line.split(" ", 1) for line in done.stdout.splitlines())
    return {k: v for k, v in options if k in {"identityagent", "batchmode"}}


@pytest.mark.skipif(not Path("/usr/bin/ssh").exists(), reason="needs /usr/bin/ssh")
@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ([], {}, {"identityagent": "none", "batchmode": "no"}),
        ([], {"SSH_AUTH_SOCK": ""}, {"identityagent": "none", "batchmode": "no"}),
        (
            [],
            {"SSH_AUTH_SOCK": "/x/1Password/t/agent.sock"},
            {"identityagent": "none", "batchmode": "no"},
        ),
        (
            [],
            {"SSH_AUTH_SOCK": "/x/other.sock"},
            {"identityagent": "/x/other.sock", "batchmode": "no"},
        ),
        (
            [],
            {"SSH_AUTH_SOCK": "/x/with space/agent.sock"},
            {"identityagent": "/x/with space/agent.sock", "batchmode": "no"},
        ),
        (
            ["-o", "IdentityAgent=/y/mine.sock"],
            {},
            {"identityagent": "none", "batchmode": "no"},
        ),
        (["-o", "BatchMode=yes"], {}, {"identityagent": "none", "batchmode": "yes"}),
        (
            ["-voIdentityAgent=/z/a.sock"],
            {},
            {"identityagent": "none", "batchmode": "no"},
        ),
    ],
)
def test_the_ssh_shim_options(
    world: World,
    tmp_path: Path,
    args: list[str],
    env: dict[str, str],
    expected: dict[str, str],
) -> None:
    """The shim sets the agent from SSH_AUTH_SOCK; BatchMode stays the caller's."""
    del world
    assert shims.install_ssh_shim(dry_run=False) == 0
    options = shim_ssh_options(tmp_path, args, env)
    assert options == expected


@pytest.mark.skipif(not Path("/usr/bin/ssh").exists(), reason="needs /usr/bin/ssh")
def test_the_ssh_shim_disables_a_symlink_to_1password(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del world
    assert shims.install_ssh_shim(dry_run=False) == 0
    monkeypatch.chdir(tmp_path)
    target = Path("1Password/agent.sock")
    target.parent.mkdir()
    alias = tmp_path / "agent.sock"
    with socket.socket(socket.AF_UNIX) as agent:
        agent.bind(str(target))
        alias.symlink_to(target)
        options = shim_ssh_options(tmp_path, [], {"SSH_AUTH_SOCK": str(alias)})
    assert options["identityagent"] == "none"


def test_the_ssh_shim_without_another_ssh_exits_127(
    world: World, tmp_path: Path
) -> None:
    del world
    assert shims.install_ssh_shim(dry_run=False) == 0
    done = subprocess.run(
        ["/bin/sh", str(gh_shim.bin_dir() / "ssh")],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(gh_shim.bin_dir()), "HOME": str(tmp_path)},
        timeout=10,  # a shim that re-executes itself never returns
    )
    assert done.returncode == 127
    assert "no ssh on PATH" in done.stderr


def test_the_ssh_shim_skips_a_symlink_to_itself(world: World, tmp_path: Path) -> None:
    """A second PATH entry that links to the shim must not make it loop."""
    del world
    assert shims.install_ssh_shim(dry_run=False) == 0
    link_dir = tmp_path / "link"
    real_dir = tmp_path / "real"
    link_dir.mkdir()
    real_dir.mkdir()
    (link_dir / "ssh").symlink_to(gh_shim.bin_dir() / "ssh")
    real = real_dir / "ssh"
    real.write_text("#!/bin/sh\necho real\n")
    real.chmod(0o755)
    done = subprocess.run(
        ["/bin/sh", str(gh_shim.bin_dir() / "ssh")],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{gh_shim.bin_dir()}:{link_dir}:{real_dir}",
            "HOME": str(tmp_path),
        },
        timeout=10,  # a shim that re-executes itself never returns
    )
    assert (done.returncode, done.stdout) == (0, "real\n")


def test_install_ssh_shim_leaves_a_foreign_ssh(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    path = gh_shim.bin_dir() / "ssh"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\necho mine\n")
    assert shims.install_ssh_shim(dry_run=False) == 1
    assert "is not ours; left alone" in capsys.readouterr().err
    assert path.read_text() == "#!/bin/sh\necho mine\n"
