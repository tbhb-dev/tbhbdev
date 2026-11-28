"""Exercise the installer script against a legacy install in a temporary home."""

import os
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.install import cli_build, command

if TYPE_CHECKING:
    from pathlib import Path

REVISION = "a" * 40
OLD = "b" * 40
URL = "https://example.com/project"
OLD_ENV = ".local/share/uv/tools/tbhbdev"
BUILDS = ".local/share/tbhbdev/builds"
LOG = ".local/state/tbhbdev/install-cli.log"

FAKE_UV = r"""#!/bin/sh
printf '%s\n' "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" "$@" > "$HOME/uv.args"
echo 'output from uv' >&2
test "$INSTALL_FAIL" != uv || exit 1
mkdir -p "$UV_TOOL_BIN_DIR" "$UV_TOOL_DIR/tbhbdev/bin"
ln -s "$HOME/proof-python" "$UV_TOOL_DIR/tbhbdev/bin/python"
cat > "$UV_TOOL_BIN_DIR/tbhbdev" <<EOF
#!/bin/sh
if test "\$1" = --version; then echo "tbhbdev 0 ($TEST_REVISION)"
elif test -n "${ADMITTED_RC:-}"; then
  echo 'agent admission slot must be owned by the current uid' >&2
  exit "${ADMITTED_RC}"
else echo "${ADMITTED:-0}"; fi
EOF
chmod +x "$UV_TOOL_BIN_DIR/tbhbdev"
"""


def executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    live = tmp_path / ".local/bin/tbhbdev"
    legacy = tmp_path / OLD_ENV
    old = legacy / "bin/tbhbdev"
    live.parent.mkdir(parents=True)
    old.parent.mkdir(parents=True)
    executable(old, f'#!/bin/sh\necho "tbhbdev 0 ({OLD})"\n')
    live.symlink_to(old)
    (legacy / "uv-receipt.toml").write_text(
        "[tool]\nrequirements = [\n"
        '  { name = "tbhbdev" },\n'
        '  { name = "one", extras = ["feature"], specifier = ">=2" },\n'
        '  { name = "two", git = "https://example.com/two?subdirectory=cli&rev=xyz" },'
        "\n"
        "]\n"
    )
    modules = tmp_path / "modules"
    alpha = modules / "tbhbdev/alpha"
    alpha.mkdir(parents=True)
    (modules / "tbhbdev/__init__.py").touch()
    table = {"first": ("math", "sqrt"), "last": ("last", "main")}
    (alpha / "command.py").write_text(f"COMMAND_MODULES = {table!r}\n")
    (modules / "last.py").write_text("main = None\n")
    proof = f"import sys; sys.path.insert(0, {str(modules)!r}); exec(sys.argv[-1])"
    interpreter = shlex.join([sys.executable, "-I", "-c", proof])
    executable(tmp_path / "proof-python", f'#!/bin/sh\nexec {interpreter} "$@"\n')
    executable(live.with_name("uv"), FAKE_UV)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{live.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TEST_REVISION", REVISION)
    return tmp_path


def run_install() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", cli_build.script(REVISION, URL)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_carries_over_receipt_requirements(home: Path) -> None:
    assert run_install().returncode == 0
    build = home / BUILDS / REVISION
    assert (home / "uv.args").read_text().splitlines() == [
        str(build),
        str(build / "bin"),
        "tool",
        "install",
        "--no-config",
        f"git+{URL}@{REVISION}#subdirectory=factory/cli",
        "--with",
        "one[feature]>=2",
        "--with",
        "two @ git+https://example.com/two@xyz#subdirectory=cli",
    ]


def test_cli_switches_live_link(home: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert command.main(["cli", REVISION], "tbhbdev alpha install") == 0
    live = home / ".local/bin/tbhbdev"
    assert live.readlink() == home / BUILDS / REVISION / "bin/tbhbdev"
    old = home / OLD_ENV / "bin/tbhbdev"
    assert (home / BUILDS / "previous").readlink() == old
    assert old.exists()
    assert capfd.readouterr().out.split()[:3] == [OLD, REVISION, "ok"]


@pytest.mark.parametrize("failure", ["uv", "import", "revision"])
def test_failed_build_keeps_live_link(
    home: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("INSTALL_FAIL", failure)
    monkeypatch.setenv("TEST_REVISION", OLD if failure == "revision" else REVISION)
    if failure == "import":
        (home / "modules/last.py").unlink()
    result = run_install()
    assert result.returncode != 0
    assert result.stdout.split()[:3] == [OLD, OLD, "failed"]
    assert (home / ".local/bin/tbhbdev").readlink() == home / OLD_ENV / "bin/tbhbdev"
    assert "output from uv" in (home / LOG).read_text()
    if failure == "revision":
        monkeypatch.setenv("TEST_REVISION", REVISION)
        assert run_install().returncode == 0


@pytest.mark.parametrize(
    ("count", "rc", "remaining"),
    [("0", "", 3), ("1", "", 5), ("error", "", 5), ("0", "1", 5)],
)
def test_prunes_only_with_zero_admitted_runs(
    home: Path, monkeypatch: pytest.MonkeyPatch, count: str, rc: str, remaining: int
) -> None:
    builds = home / BUILDS
    for number in range(4):
        build = builds / f"{number:040x}"
        build.mkdir(parents=True)
        os.utime(build, (number, number))
    monkeypatch.setenv("ADMITTED", count)
    monkeypatch.setenv("ADMITTED_RC", rc)
    assert run_install().returncode == 0
    assert len([build for build in builds.iterdir() if build.is_dir()]) == remaining
    assert "Traceback" not in (home / LOG).read_text()
    for revision in (f"{2:040x}", f"{3:040x}", REVISION):
        assert (builds / revision).is_dir()
