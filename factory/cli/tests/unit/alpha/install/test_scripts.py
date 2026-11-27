"""Tests for rendering Git hook and shell scripts."""

from pathlib import PurePath

from tbhbdev.alpha.install.scripts import SHIM_MARK, identity_shim, path_export


def test_identity_shim_substitutes_the_hook_and_agent_values() -> None:
    script = identity_shim(
        "commit-msg", "check", "example", ("SESSION",), "agent@example.test"
    )
    assert script.startswith(f"#!/bin/sh\n{SHIM_MARK}\n")
    assert 'tbhbdev alpha identity check "$@"' in script
    assert "${SESSION:-}" in script
    assert "'agent@example.test'" in script
    assert "grep -Eqi '^example$'" in script
    assert "/commit-msg.prek" in script


def test_path_export_preserves_existing_lines_and_is_idempotent() -> None:
    directory = PurePath("/path with spaces")
    line = """export PATH='/path with spaces'":$PATH"\n"""
    assert path_export(directory, "") == line
    assert path_export(directory, "export A=1\n") == line
    assert path_export(directory, "export A=1") == "\n" + line
    assert path_export(directory, f"export A=1\n{line}") == ""
