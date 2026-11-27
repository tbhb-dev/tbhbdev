# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from design_report import read_tree
from markdown import difference
from metrics import measure


class DesignReportTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "src/pkg").mkdir(parents=True)
        (self.root / "tests").mkdir()

    def write(self, path: str, text: str) -> None:
        (self.root / path).write_text(text, encoding="utf-8")

    def snapshot(self):
        return measure(read_tree(self.root / "src"), read_tree(self.root / "tests"))

    def test_dependency_graph(self) -> None:
        self.write("src/pkg/__init__.py", '"""Package."""\n')
        self.write("src/pkg/b.py", "def f(): pass\n")
        self.write("src/pkg/types.py", "")
        self.write(
            "src/pkg/a.py",
            """
from typing import TYPE_CHECKING as CHECKING
if CHECKING:
    from . import types
else:
    from . import b
""",
        )
        self.write("tests/test_a.py", "import pkg.a\n")
        self.write("tests/test_b.py", "import pkg.b\nimport pkg.b\n")
        report = self.snapshot()
        modules = report["modules"]
        self.assertEqual(modules["pkg.a"]["fan_out"], 2)
        self.assertEqual(modules["pkg.b"]["fan_in"], 1)
        self.assertEqual(modules["pkg.b"]["instability"], 0)
        self.assertEqual(modules["pkg.a"]["instability"], 1)
        self.assertEqual(modules["pkg.__init__"]["fan_in"], 1)
        self.assertEqual(modules["pkg.__init__"]["test_files"], 2)
        self.assertEqual(modules["pkg.b"]["test_files"], 2)
        self.assertEqual(modules["pkg.types"]["fan_in"], 0)
        self.assertEqual(report["totals"]["cumulative_component_dependency"], 2)
        self.assertEqual(report["totals"]["initializers_with_statements"], 0)

    def test_difference(self) -> None:
        self.write("src/pkg/a.py", "def f(): pass\n")
        self.write("src/pkg/stable.py", "pass\n")
        base = self.snapshot()
        self.assertEqual(difference(base, base), "No design metrics changed.\n")
        self.write("src/pkg/__init__.py", '"""Package."""\nvalue = 1\n')
        self.write("src/pkg/a.py", "def f(): pass\n" + "\n" * 300)
        self.write("src/pkg/new.py", "# Comment\n" * 301)
        report = difference(base, self.snapshot())
        self.assertIn("| modules over 300 lines | 0 | 2 | +2 |", report)
        self.assertIn("| initializers with statements | 0 | 1 | +1 |", report)
        self.assertNotIn("pkg.stable", report)
        self.assertLess(report.index("pkg.a"), report.index("pkg.__init__"))
        limits = report.split("## New modules over limits")[1]
        self.assertIn("pkg.__init__", limits)
        self.assertIn("pkg.new", limits)
        self.assertNotIn("pkg.a", limits)
