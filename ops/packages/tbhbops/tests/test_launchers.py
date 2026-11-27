"""Run each tools/ launcher the way a checkout does, through uv.

A launcher reaches tbhbops through a path source in its inline metadata, and
uv caches the script's environment by that metadata. A non-editable source
froze tbhbops at the first run, so a module added later raised
ModuleNotFoundError until the cache was cleared. These tests run each
launcher in a copy of the repository with an empty uv cache, then add a
module and run a launcher with the same metadata that imports it.
"""

import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import uv
from tbhbops import op_connect

ROOT = next(
    path
    for path in Path(__file__).parents
    if (path / "tools").is_dir() and (path / "packages" / "tbhbops").is_dir()
)
LAUNCHERS = sorted(path.name for path in (ROOT / "tools").iterdir() if path.is_file())
# The end of the inline metadata block.
METADATA_END = "# ///\n"
EDITABLE = '# tbhbops = { path = "../packages/tbhbops", editable = true }\n'
CA_ROOT = "apps/ca/roots/root_ca.crt"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return a copy of what the launchers read, with no uv state."""
    copy = tmp_path / "repo"
    for part in ("apps", "tools", "packages/tbhbops"):
        shutil.copytree(
            ROOT / part,
            copy / part,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
        )
    return copy


def uv_bin() -> str:
    if "TEST_SRCDIR" in os.environ:
        # rules_python places wheel scripts beside site-packages.
        executable = "uv.exe" if sys.platform == "win32" else "uv"
        return str(Path(uv.__file__).parents[2] / "bin" / executable)
    return uv.find_uv_bin()


def uv_run(repo: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run tools/<script> through uv, offline, with the repo's own uv cache."""
    # Nothing from the test's own environment that picks a Python, a package
    # or a uv setting: mutmut's PYTHONPATH would import its mutated tbhbops
    # ahead of the copy.
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PYTHON", "UV_", "VIRTUAL_ENV"))
        },
        "UV_CACHE_DIR": str(repo.parent / "uv-cache"),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_CONFIG": "1",
    }
    return subprocess.run(
        [
            uv_bin(),
            "run",
            "--quiet",
            "--python",
            sys.executable,
            "--script",
            str(repo / "tools" / script),
            *args,
        ],
        capture_output=True,
        check=False,
        cwd=repo,
        env=env,
        text=True,
        timeout=120,
    )


def test_there_are_launchers() -> None:
    assert LAUNCHERS


@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_the_launcher_reaches_tbhbops_editable(launcher: str) -> None:
    text = (ROOT / "tools" / launcher).read_text(encoding="utf-8")
    assert EDITABLE in text


@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_the_launcher_sees_a_module_added_after_its_first_run(
    repo: Path, launcher: str
) -> None:
    if launcher == "install-step-ca-root":
        # This launcher has no help option. Stop at validation on macOS so
        # the first run can populate the uv cache without using the keychain.
        root = repo / CA_ROOT
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_bytes(b"")
        first = uv_run(repo, launcher)
        message = (
            "The root file must contain exactly one PEM certificate."
            if sys.platform == "darwin"
            else "This command requires macOS."
        )
        assert first.returncode == 1, first.stderr
        assert first.stderr == f"{message}\n"
    else:
        first = uv_run(repo, launcher, "--help")
        assert first.returncode == 0, first.stderr
        assert re.match(rf"usage: {re.escape(launcher)} ", first.stdout)

    module = repo / "packages" / "tbhbops" / "src" / "tbhbops" / "added_later.py"
    module.write_text('MESSAGE = "added later"\n', encoding="utf-8")
    text = (repo / "tools" / launcher).read_text(encoding="utf-8")
    metadata = text[: text.index(METADATA_END) + len(METADATA_END)]
    # Same metadata, so uv reuses the environment the first run built.
    probe = repo / "tools" / f"{launcher}-probe"
    probe.write_text(
        metadata + "from tbhbops.added_later import MESSAGE\nprint(MESSAGE)\n",
        encoding="utf-8",
    )
    second = uv_run(repo, probe.name)
    assert second.returncode == 0, second.stderr
    assert second.stdout == "added later\n"


@pytest.mark.parametrize(
    ("defaults_text", "message"),
    [
        (
            "ca_client_trust_root_sha256: >-\n  {fingerprint}\n",
            "System keychain installation failed: test stopped at keychain lookup",
        ),
        (
            "ca_client_trust_root_sha256: >-\n  AB:CD\n",
            "The CA trust defaults lack a valid ca_client_trust_root_sha256 pin.",
        ),
        (
            "ca_client_trust_system_keychain: false\n",
            "The CA trust defaults lack a valid ca_client_trust_root_sha256 pin.",
        ),
    ],
)
def test_step_ca_root_pin(
    repo: Path, monkeypatch: pytest.MonkeyPatch, defaults_text: str, message: str
) -> None:
    root = repo / CA_ROOT
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).parent / "testdata" / "root_ca.crt", root)
    fingerprint = ":".join(["AB"] * 32)
    defaults = repo / "ansible/roles/ca_client_trust/defaults/main.yml"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(defaults_text.format(fingerprint=fingerprint), encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        op_connect,
        "run_subprocess",
        Mock(
            side_effect=[
                f"SHA256 Fingerprint={fingerprint}\n".encode(),
                OSError("test stopped at keychain lookup"),
            ],
        ),
    )
    with pytest.raises(SystemExit, match=rf"^{re.escape(message)}$"):
        runpy.run_path(str(repo / "tools/install-step-ca-root"), run_name="__main__")
