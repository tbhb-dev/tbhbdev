# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# gazelle:include_pytest_conftest false

"""Tests for selecting staged Markdown documents."""

from tbhbdev.alpha.identity.documents.selection import stampable_docs


def test_raw_diff_selects_nested_markdown_with_its_staged_mode_and_blob() -> None:
    meta = ":100644 100755 old new M"
    diff = "".join(
        f"{meta}\0{path}\0"
        for path in ("README.md", "docs/A B.MD", "docs/index.md", "docs/testdata/a.md")
    )
    assert stampable_docs(diff) == [("docs/A B.MD", "100755", "new")]
