"""Exercise CLI lint failures and the paths for shrinking the baseline."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from check_cli_lint import CLI, CONFIG, violations

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def sample() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir=CLI / "src") as directory:
        yield Path(directory).relative_to(Path.cwd()) / "sample.py"


def test_module_size(sample: Path) -> None:
    name = sample.relative_to(CLI).as_posix()
    sample.write_text("# a line\n" * 302, encoding="utf-8")
    assert "ceiling 300" in violations([sample], {}, {}, 300)[0]
    assert "ceiling 301" in violations([sample], {name: 301}, {}, 300)[0]
    sample.write_text("# a line\n" * 301, encoding="utf-8")
    assert violations([sample], {name: 301}, {}, 300) == []
    sample.write_text("# a line\n" * 300, encoding="utf-8")
    assert violations([sample], {}, {}, 300) == []


def test_suppressions(sample: Path) -> None:
    sample.write_text("pass  # noqa: S105, S106\n", encoding="utf-8")
    assert "S106" in violations([sample], {}, {"S105": 1}, 300)[0]
    sample.write_text('text = "# noqa: S106"\n', encoding="utf-8")
    assert violations([sample], {}, {"S105": 1}, 300) == []
    sample.write_text("# ruff: noqa\n", encoding="utf-8")
    assert "all" in violations([sample], {}, {}, 300)[0]


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
        assert edge in stale.stdout + stale.stderr
    finally:
        path.write_text(original, encoding="utf-8")
        CONFIG.write_text(config, encoding="utf-8")

    shrunk = lint()
    assert shrunk.returncode == 0, shrunk.stdout + shrunk.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
