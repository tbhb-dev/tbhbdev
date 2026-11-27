"""Tests for install arguments."""

from tbhbdev.alpha.install.arguments import build_parser


def test_help() -> None:
    help_text = build_parser("tbhbdev alpha install").format_help()
    assert help_text.startswith(
        "usage: tbhbdev alpha install [-h] [--dry-run | --hook]\n"
    )
    assert "Install this clone's git hooks" in help_text
    assert "print the changes, write nothing" in help_text
    assert "SessionStart hook: put the gh shim on PATH" in help_text
