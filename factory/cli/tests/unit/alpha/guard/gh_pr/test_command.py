# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise the pull request hook and its CLI integration."""

import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tbhbdev.alpha import close, command, install, pull_request, squash_merge
from tbhbdev.alpha.guard.gh_pr import command as hook
from tbhbdev.alpha.guard.gh_pr import decision as guard

DATA = Path(__file__).parents[4] / "testdata" / "alpha"


CREATE = (
    "`gh pr create` is blocked. Write .github/PULL_REQUEST_DRAFT.md from "
    ".github/PULL_REQUEST_TEMPLATE.md, check it with `tbhbdev alpha pr check`, "
    "then open the PR with `tbhbdev alpha pr create`."
)


MERGE = (
    "`gh pr merge` is blocked. Once the PR is approved, run "
    "`tbhbdev alpha merge {n}` (add `--dry-run` to preview the commit message first)."
)


def run_find(text: str) -> guard.Match | None:
    """Scan one command."""
    return guard.find(text)


CLAUDE_PAYLOAD = json.loads((DATA / "claude-pretooluse-bash-main.json").read_text())


CODEX_PAYLOAD = json.loads((DATA / "codex-pretooluse-bash.json").read_text())


AGY_PAYLOAD = json.loads((DATA / "agy-pretooluse-run-command.json").read_text())


AGY_WRITE = json.loads((DATA / "agy-pretooluse-write.json").read_text())


def with_command(payload: dict[str, object], text: str) -> dict[str, object]:
    """A recorded payload with its shell command replaced."""
    if "toolCall" in payload:
        call = cast("dict[str, object]", payload["toolCall"])
        args = cast("dict[str, object]", call["args"])
        return {**payload, "toolCall": {**call, "args": {**args, "CommandLine": text}}}
    inputs = cast("dict[str, object]", payload["tool_input"])
    return {**payload, "tool_input": {**inputs, "command": text}}


@pytest.fixture(autouse=True)
def tbhb_dev(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """Every payload's repository counts as a tbhb-dev one; record each ask."""
    asked: list[Path | None] = []

    def in_tbhb_dev(cwd: Path | None = None) -> bool:
        asked.append(cwd)
        return True

    monkeypatch.setattr(hook, "in_tbhb_dev", in_tbhb_dev)
    return asked


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
) -> tuple[int, str, str]:
    """Run ``main`` on stdin; return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = hook.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD])
def test_claude_and_codex_deny_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "cd x && gh pr create"))
    code, out, err = run_main(monkeypatch, capsys, stdin)
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{guard.HINT_LINE}\n{CREATE}\n{guard.FOOTER}",
        }
    }


def test_antigravity_deny_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin = json.dumps(with_command(AGY_PAYLOAD, "gh pr merge 4"))
    code, out, err = run_main(monkeypatch, capsys, stdin)
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "decision": "deny",
        "reason": f"{guard.HINT_LINE}\n{MERGE.format(n=4)}\n{guard.FOOTER}",
    }


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_recorded_payloads_allow_their_own_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    code, out, err = run_main(monkeypatch, capsys, json.dumps(payload))
    assert (code, out, err) == (0, "", "")


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_gh_pr_view_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "gh pr view 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_agy_non_command_tool_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(monkeypatch, capsys, json.dumps(AGY_WRITE)) == (0, "", "")


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "null",
        "[1]",
        "{}",
        '{"tool_input": null}',
        '{"tool_input": {"command": 7}}',
        '{"tool_input": []}',
        '{"toolCall": null}',
        '{"toolCall": {"args": null}}',
        '{"toolCall": {"args": {"CommandLine": 7}}}',
        '{"toolCall": {"args": "gh pr merge 1"}}',
    ],
)
def test_malformed_input_fails_open_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_internal_error_fails_open_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_text: str, *, in_repository: bool) -> str | None:
        assert in_repository
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(hook, "repository_message", boom)
    stdin = json.dumps(with_command(CLAUDE_PAYLOAD, "gh pr merge 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_the_gate_asks_about_the_payloads_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tbhb_dev: list[Path | None],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "gh pr merge 1"))
    run_main(monkeypatch, capsys, stdin)
    assert tbhb_dev == [Path("/nonexistent/work")]


def test_outside_tbhb_dev_even_a_blocked_command_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def in_tbhb_dev(_cwd: Path | None) -> bool:
        return False

    monkeypatch.setattr(hook, "in_tbhb_dev", in_tbhb_dev)
    stdin = json.dumps(with_command(CLAUDE_PAYLOAD, "gh pr merge 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


@given(st.text(max_size=80))
def test_property_main_is_silent_without_a_match(text: str) -> None:
    payload = json.dumps({"tool_input": {"command": text}})
    found = run_find(text)
    out = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(out):
        assert hook.main() == 0
    assert (out.getvalue() != "") == (found is not None)


def guard_groups(groups: list[dict[str, object]]) -> list[dict[str, object]]:
    """The PreToolUse groups that run the guard."""
    return [
        g
        for g in groups
        if any(
            "alpha guard-gh-pr" in h["command"]
            for h in cast("list[dict[str, str]]", g["hooks"])
        )
    ]


@pytest.mark.parametrize(
    ("groups", "matcher"),
    [
        (install.claude_hooks()["PreToolUse"], "Bash"),
        (install.codex_hooks()["PreToolUse"], "^Bash$"),
        (
            cast(
                "dict[str, list[dict[str, object]]]",
                install.agy_hooks()["tbhbdev-alpha-pull-request"],
            )["PreToolUse"],
            "run_command",
        ),
    ],
)
def test_every_harness_config_runs_the_guard(
    groups: list[dict[str, object]], matcher: str
) -> None:
    (group,) = guard_groups(groups)
    assert group["matcher"] == matcher
    (handler,) = cast("list[dict[str, str]]", group["hooks"])
    assert handler["command"] == "tbhbdev alpha guard-gh-pr # tbhbdev alpha install"


GH_ROUTES = [
    ("gh pr review 7 --approve -R tbhb-dev/tbhbdev", "`gh pr review --approve`"),
    (
        "gh api -X POST repos/tbhb-dev/tbhbdev/pulls/7/reviews -f event=APPROVE",
        "a `gh api` APPROVE review",
    ),
]


CLAUDE_CHILD = json.loads((DATA / "claude-pretooluse-bash-child.json").read_text())


def reason(out: str) -> str | None:
    """The deny reason of a Claude or Codex decision; None for silence."""
    if not out:
        return None
    output = json.loads(out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    return cast("str", output["permissionDecisionReason"])


def guard_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
    text: str,
) -> str | None:
    code, out, err = run_main(
        monkeypatch, capsys, json.dumps(with_command(payload, text))
    )
    assert (code, err) == (0, "")
    return reason(out)


GH_DENIED = (
    "An agent can't approve a pull request through gh, whoever opened it. "
    "Review runs post the approving review. "
    "Use a COMMENT review to leave feedback directly. The hook blocks {route}."
)


@pytest.mark.parametrize(("text", "route"), GH_ROUTES)
@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CLAUDE_CHILD, CODEX_PAYLOAD])
def test_no_session_approves_through_gh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
    text: str,
    route: str,
) -> None:
    expected = GH_DENIED.format(route=route)
    assert guard_reason(monkeypatch, capsys, payload, text) == expected


def test_retired_approval_command_is_denied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "tbhb-" + "approve tools 7 abc123"
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, text) == GH_DENIED.format(
        route="the retired approval command"
    )


def test_a_crash_while_matching_an_approval_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_text: str) -> str | None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(hook, "approval_message", boom)
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, GH_ROUTES[0][0]) is None
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, "gh pr merge 1") == (
        f"{guard.HINT_LINE}\n{MERGE.format(n=1)}\n{guard.FOOTER}"
    )


def test_approval_rules_apply_outside_tbhb_dev(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def in_tbhb_dev(_cwd: Path | None) -> bool:
        return False

    monkeypatch.setattr(hook, "in_tbhb_dev", in_tbhb_dev)
    text, route = GH_ROUTES[0]
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, text) == GH_DENIED.format(
        route=route
    )
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, "gh pr merge 1") is None


PLACEHOLDERS = {
    "N": "7",
    "<N>": "7",
    "TEXT": "why",
    "completed|not-planned": "completed",
}


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create",
        "gh pr edit 3",
        "gh pr merge 3",
        "gh pr ready 3",
        "gh pr close 3",
        "gh issue close 3",
        "gh run rerun 9",
        "gh api -X DELETE repos/a/b/git/refs/heads/x",
    ],
)
def test_every_hinted_command_line_parses_with_the_real_cli(command: str) -> None:
    found = run_find(command)
    assert found is not None
    spans = re.findall(r"`tbhbdev alpha ([^`]+)`", guard.hint(found))
    assert spans
    for span in spans:
        words = [
            PLACEHOLDERS.get(w, w) for w in re.sub(r"\s*\[[^\]]*\]", "", span).split()
        ]
        if words[0] == "merge":
            squash_merge.build_parser("x").parse_args(words[1:])
        elif words[:2] in (["pr", "close"], ["issue", "close"]):
            close.build_parser("x").parse_args(words)
        else:
            pull_request.build_parser("x").parse_args(
                words[1:] if words[0] == "pr" else words
            )


def test_cli_dispatches_guard_gh_pr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.dumps(with_command(CLAUDE_PAYLOAD, "gh pr merge 1"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert command.main(["guard-gh-pr"], "tbhbdev alpha") == 0
    assert "hookSpecificOutput" in json.loads(capsys.readouterr().out)
