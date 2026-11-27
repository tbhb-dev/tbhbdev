# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

from pathlib import Path
from unittest import TestCase

from .design_report import read_tree
from .markdown import difference
from .metrics import measure

FIXTURES = Path(__file__).with_name("testdata")


class DesignReportTest(TestCase):
    def test_dependency_graph(self) -> None:
        sources = read_tree(FIXTURES / "src")
        report = measure(sources, {"test_a": "import pkg.a", "test_b": "import pkg.b"})
        modules = report["modules"]
        a, b, init = (modules[f"pkg.{name}"] for name in ("a", "b", "__init__"))
        assert (a["fan_out"], a["instability"]) == (2, 1)
        assert (b["fan_in"], b["test_files"]) == (1, 2)
        assert (init["fan_in"], init["test_files"]) == (1, 2)
        assert report["totals"]["cumulative_component_dependency"] == 2
        assert report["totals"]["initializers_with_statements"] == 0

    def test_difference(self) -> None:
        base = measure({"pkg.a": "pass"}, {})
        assert "| Any | 0 | 0 | +0 |" in difference(base, base)
        head = measure({"pkg.a": "pass\n" * 301, "pkg.__init__": "value = 1"}, {})
        report = difference(base, head)
        assert "| modules over 300 lines | 0 | 1 | +1 |" in report
        assert "| initializers with statements | 0 | 1 | +1 |" in report
        limits = report.split("## New modules over limits")[1]
        assert "pkg.__init__" in limits
        assert "pkg.a" not in limits

    def test_annotations(self) -> None:
        fixture = FIXTURES / "annotations.txt"
        imports = "import typing as t\nfrom collections.abc import Mapping as M\n"
        for line in fixture.read_text(encoding="utf-8").splitlines():
            expected_any, expected_mapping, source = line.split(" ", 2)
            with self.subTest(source=source):
                text = imports + source.replace(r"\n", "\n")
                totals = measure({"sample": text}, {})["totals"]
                assert totals["Any"] == int(expected_any)
                assert totals["dict_str_object_annotations"] == int(expected_mapping)
