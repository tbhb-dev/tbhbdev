"""Tests for install hooks."""

from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev.alpha.install import hooks, settings

if TYPE_CHECKING:
    from tbhbdev.alpha.install.model import Json


def commands(groups: list[Json]) -> list[str]:
    return [
        cast("str", h["command"])
        for g in groups
        for h in cast("list[Json]", g["hooks"])
    ]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"command": hooks.command("old")}, True),
        ({"command": "their-hook"}, False),
        ({"command": f"x {hooks.MARK} y"}, False),
        ({"command": 3}, False),
        ({}, False),
        ("text", False),
    ],
)
def test_ours(entry: object, *, expected: bool) -> None:
    assert hooks.ours(entry) is expected


def test_every_command_is_marked_and_runs_tbhbdev_alpha_from_path() -> None:
    every = [
        *(c for gs in hooks.claude_hooks().values() for c in commands(gs)),
        *(c for gs in hooks.codex_hooks().values() for c in commands(gs)),
    ]
    agy = cast("dict[str, dict[str, list[Json]]]", hooks.agy_hooks())
    for events in agy.values():
        for event, entries in events.items():
            flat = entries if event in {"SessionStart", "Stop"} else None
            every += (
                [cast("str", h["command"]) for h in flat] if flat else commands(entries)
            )
    assert every
    for text in every:
        assert text.startswith("tbhbdev alpha ")
        assert text.endswith(f" {hooks.MARK}")


def test_claude_hooks() -> None:
    record = {"type": "command", "command": hooks.command("identity record claude")}
    guard = {"type": "command", "command": hooks.command("state-guard"), "timeout": 10}
    context = {
        "type": "command",
        "command": hooks.command("session-context"),
        "timeout": 10,
    }
    assert hooks.claude_hooks() == {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": hooks.command("run-stop-hook claude"),
                        "timeout": 10,
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": hooks.command("install --hook")},
                    record,
                    guard,
                    context,
                ]
            }
        ],
        "SubagentStart": [{"hooks": [record]}],
        "PreToolUse": [
            {"matcher": "Bash|Agent", "hooks": [record]},
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": hooks.command("guard-gh-pr")}],
            },
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": hooks.command("guard-op")}],
            },
            {
                "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command", "command": hooks.command("guard-op")}],
            },
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": hooks.command("guard-gh-poll")}
                ],
            },
            {
                "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                "hooks": [
                    {"type": "command", "command": hooks.command("guard-gh-poll")}
                ],
            },
            {
                "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
                "hooks": [
                    {"type": "command", "command": hooks.command("guard-scripts")}
                ],
            },
            {"matcher": "Bash|Write|Edit", "hooks": [guard]},
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": hooks.command("guard-gh-config-dir")}
                ],
            },
            {
                "matcher": "Agent",
                "hooks": [
                    {"type": "command", "command": hooks.command("guard-agent-model")}
                ],
            },
        ],
        "PostToolUse": [
            {"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash", "hooks": [record]},
            {
                "matcher": "Write|Edit|MultiEdit",
                "hooks": [
                    {"type": "command", "command": hooks.command("pr hook claude")}
                ],
            },
        ],
        "PostToolUseFailure": [{"matcher": "Bash", "hooks": [record]}],
        "PostModelSwitch": [{"hooks": [record]}],
    }


def test_the_harness_guards_are_ours_and_a_rerun_replaces_them() -> None:
    desired = hooks.claude_hooks()
    theirs = {"type": "command", "command": "theirs"}
    old: Json = {"hooks": {"Stop": [{"hooks": [theirs]}]}}
    once = settings.merge_events(old, desired)
    assert settings.merge_events(once, desired) == once
    events = cast("dict[str, list[Json]]", once["hooks"])
    assert events["Stop"] == [{"hooks": [theirs]}, *desired["Stop"]]
    guards = [
        h
        for groups in events.values()
        for g in groups
        for h in cast("list[Json]", g["hooks"])
        if cast("str", h["command"]).startswith("tbhbdev alpha state-guard")
    ]
    assert len(guards) == 2
    assert all(settings.ours(h) and h["timeout"] == 10 for h in guards)


def test_codex_hooks() -> None:
    def one(text: str) -> Json:
        return {"type": "command", "command": hooks.command(text), "timeout": 600}

    record = one("identity record codex")
    assert hooks.codex_hooks() == {
        "Stop": [{"hooks": [one("run-stop-hook codex")]}],
        "SessionStart": [{"hooks": [record]}],
        "SubagentStart": [{"hooks": [record]}],
        "PostToolUse": [
            {"matcher": "apply_patch", "hooks": [record]},
            {"matcher": "apply_patch", "hooks": [one("pr hook codex")]},
        ],
        "PreToolUse": [
            {"matcher": "^Bash$", "hooks": [one("guard-gh-pr")]},
            {"matcher": "^Bash$", "hooks": [one("guard-gh-poll")]},
            {"matcher": "^Bash$", "hooks": [one("guard-scripts")]},
            {"matcher": "^apply_patch$", "hooks": [one("guard-scripts")]},
            {"matcher": "^(Bash|shell)$", "hooks": [one("guard-op")]},
            {"matcher": "apply_patch", "hooks": [one("guard-op")]},
            {"matcher": "^Bash$", "hooks": [one("guard-gh-config-dir")]},
        ],
    }


def test_agy_hooks() -> None:
    edits = (
        "write_file|edit_file|write_to_file|replace_file_content"
        "|multi_replace_file_content"
    )
    record = {
        "type": "command",
        "command": hooks.command("identity record antigravity"),
        "timeout": 30,
    }
    assert hooks.agy_hooks() == {
        "tbhbdev-alpha-run-stop": {
            "Stop": [{**record, "command": hooks.command("run-stop-hook antigravity")}],
        },
        "tbhbdev-alpha-identity": {
            "SessionStart": [
                {
                    "type": "command",
                    "command": hooks.command("identity record antigravity"),
                }
            ],
            "PreToolUse": [{"matcher": "run_command", "hooks": [record]}],
            "PostToolUse": [{"matcher": edits, "hooks": [record]}],
        },
        "tbhbdev-alpha-op-account": {
            "PreToolUse": [
                {
                    "matcher": matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": hooks.command("guard-op"),
                            "timeout": 30,
                        }
                    ],
                }
                for matcher in ("run_command", edits)
            ],
        },
        "tbhbdev-alpha-pull-request": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hooks.command("guard-gh-pr"),
                            "timeout": 30,
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": edits,
                    "hooks": [
                        {
                            "type": "command",
                            "command": hooks.command("pr hook antigravity"),
                            "timeout": 120,
                        }
                    ],
                }
            ],
        },
        "tbhbdev-alpha-gh-poll": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hooks.command("guard-gh-poll"),
                            "timeout": 30,
                        }
                    ],
                }
            ],
        },
        "tbhbdev-alpha-gh-config-dir": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hooks.command("guard-gh-config-dir"),
                            "timeout": 30,
                        }
                    ],
                }
            ],
        },
        "tbhbdev-alpha-scripts": {
            "PreToolUse": [
                {
                    "matcher": "run_command|write_file",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hooks.command("guard-scripts"),
                            "timeout": 30,
                        }
                    ],
                }
            ],
        },
    }


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
        (hooks.claude_hooks()["PreToolUse"], "Bash"),
        (hooks.codex_hooks()["PreToolUse"], "^Bash$"),
        (
            cast(
                "dict[str, list[dict[str, object]]]",
                hooks.agy_hooks()["tbhbdev-alpha-pull-request"],
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
