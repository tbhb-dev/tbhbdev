"""Exercise CLI import contract violations and stale exceptions."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CLI = Path("factory/cli")
CONFIG = CLI / "pyproject.toml"


@pytest.mark.parametrize(
    ("contract", "module", "target", "statement"),
    [
        ("cli-layers", "runner", "tbhbdev.cli", "from tbhbdev import cli"),
        (
            "cli-decisions",
            "alpha.agent.decisions.argv",
            "subprocess",
            "import subprocess",
        ),
        (
            "cli-decisions",
            "alpha.identity.commands",
            "tbhbdev.alpha.identity.dispatcher",
            "from tbhbdev.alpha.identity import dispatcher",
        ),
        (
            "cli-decisions",
            "alpha.identity.models",
            "tbhbdev.alpha.identity.dispatcher",
            "from tbhbdev.alpha.identity import dispatcher",
        ),
    ],
)
def test_import_contract(
    contract: str, module: str, target: str, statement: str
) -> None:
    path = CLI / "src/tbhbdev" / (module.replace(".", "/") + ".py")
    original = path.read_text(encoding="utf-8")
    config = CONFIG.read_text(encoding="utf-8")
    edge = f"tbhbdev.{module} -> {target}"
    tool = shutil.which("import_linter_test.tool")
    assert tool is not None

    def lint() -> subprocess.CompletedProcess[str]:
        return subprocess.run([tool], text=True, capture_output=True, check=False)

    try:
        path.write_text(f"{original}\n{statement}\n", encoding="utf-8")
        failed = lint()
        assert failed.returncode == 1, failed.stdout + failed.stderr
        assert edge in failed.stdout

        before, marker, after = config.partition(f'id = "{contract}"')
        ignored = after.replace(
            "ignore_imports = [", f'ignore_imports = [\n  "{edge}",', 1
        )
        CONFIG.write_text(before + marker + ignored, encoding="utf-8")
        passed = lint()
        assert passed.returncode == 0, passed.stdout + passed.stderr

        path.write_text(original, encoding="utf-8")
        stale = lint()
        assert stale.returncode == 1, stale.stdout + stale.stderr
        diagnostic = " ".join((stale.stdout + stale.stderr).split())
        assert f"No matches for ignored import {edge}" in diagnostic
    finally:
        path.write_text(original, encoding="utf-8")
        CONFIG.write_text(config, encoding="utf-8")

    shrunk = lint()
    assert shrunk.returncode == 0, shrunk.stdout + shrunk.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
