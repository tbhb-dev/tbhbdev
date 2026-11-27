"""Test the coordinator argument parser."""

# gazelle:include_pytest_conftest false

from __future__ import annotations

import pytest

from tbhbdev.alpha.agent.driver.arguments import parser

REQUIRED = {
    "--repo": "example/repo",
    "--model": "example",
    "--effort": "high",
    "--profile": "build",
}


def exec_args(options: dict[str, str]) -> list[str]:
    flags = [part for pair in options.items() for part in pair]
    return ["exec", *flags, "sample", "brief.md"]


def test_exec_help_describes_expected_commits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        parser("tbhbdev alpha agent").parse_args(["exec", "--help"])
    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert (
        "--expect-commit, --no-expect-commit require a new commit and a push"
        in help_text
    )


def test_exec_help_lists_model_examples(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as caught:
        parser("tbhbdev alpha agent").parse_args(["exec", "--help"])
    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--harness {codex,claude,agy} harness to launch: Codex, Claude or agy" in (
        help_text
    )
    assert "--allow-non-gemini let agy run a non-Gemini model ID" in help_text
    assert "gemini-" in help_text
    assert "claude-opus-5-5" in help_text


@pytest.mark.parametrize("flag", REQUIRED)
def test_required_flags(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    options = {name: value for name, value in REQUIRED.items() if name != flag}
    with pytest.raises(SystemExit) as caught:
        parser("tbhbdev alpha agent").parse_args(exec_args(options))
    assert caught.value.code == 2
    assert f"the following arguments are required: {flag}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--harness", "bad"), ("--profile", "bad"), ("--timeout", "bad")],
)
def test_parser_rejects_invalid_values(flag: str, value: str) -> None:
    with pytest.raises(SystemExit) as caught:
        parser("tbhbdev alpha agent").parse_args(exec_args(REQUIRED | {flag: value}))
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["diff", "sample", "-n", "1"],
        ["log", "sample", "--patch"],
        ["log", "sample", "-n", "invalid"],
    ],
)
def test_inspection_rejects_extra_options(args: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        parser("tbhbdev alpha agent").parse_args(args)
    assert caught.value.code == 2
