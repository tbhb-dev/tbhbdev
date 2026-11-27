"""Tests for install settings."""

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev.alpha.install import hooks, model, settings

if TYPE_CHECKING:
    from tbhbdev.alpha.install.model import Json


def test_config_paths() -> None:
    home = Path("/example")
    assert (
        settings.config_path("claude", {}, home) == home / ".claude" / "settings.json"
    )
    assert settings.config_path("claude", {"CLAUDE_CONFIG_DIR": ""}, home) == (
        home / ".claude" / "settings.json"
    )
    assert settings.config_path("claude", {"CLAUDE_CONFIG_DIR": "/c"}, home) == Path(
        "/c/settings.json"
    )
    assert settings.config_path("codex", {}, home) == home / ".codex" / "hooks.json"
    assert settings.config_path("codex", {"CODEX_HOME": "/x"}, home) == Path(
        "/x/hooks.json"
    )
    assert settings.config_path("antigravity", {"HOME": "/elsewhere"}, home) == (
        home / ".gemini" / "config" / "hooks.json"
    )


MINE = {"type": "command", "command": hooks.command("old")}


THEIRS = {"type": "command", "command": "their-hook"}


def test_strip_ours() -> None:
    assert settings.strip_ours([]) == ([], None)
    odd: list[object] = ["text", {"matcher": "x"}, {"hooks": "x"}]
    assert settings.strip_ours(odd) == (odd, None)
    groups: list[object] = [
        {"matcher": "a", "hooks": [THEIRS]},
        {"matcher": "b", "hooks": [MINE]},
        {"matcher": "c", "hooks": [THEIRS, MINE]},
        {"matcher": "d", "hooks": [MINE, MINE]},
    ]
    assert settings.strip_ours(groups) == (
        [{"matcher": "a", "hooks": [THEIRS]}, {"matcher": "c", "hooks": [THEIRS]}],
        1,
    )
    assert settings.strip_ours([{"hooks": [MINE]}, {"hooks": [THEIRS]}]) == (
        [{"hooks": [THEIRS]}],
        0,
    )


DESIRED: dict[str, list[Json]] = {
    "Start": [{"hooks": [{"type": "command", "command": hooks.command("new")}]}],
    "Pre": [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": hooks.command("g")}],
        }
    ],
}


def test_merge_events_into_an_empty_config() -> None:
    assert settings.merge_events({}, DESIRED) == {"hooks": DESIRED}


def test_merge_events_is_idempotent_and_keeps_everything_else() -> None:
    config: Json = {
        "model": "x",
        "hooks": {
            "Stop": [{"hooks": [THEIRS]}],
            "Pre": [{"matcher": "Edit", "hooks": [THEIRS]}],
        },
        "permissions": {"allow": []},
    }
    once = settings.merge_events(config, DESIRED)
    assert once == {
        "model": "x",
        "hooks": {
            "Stop": [{"hooks": [THEIRS]}],
            "Pre": [{"matcher": "Edit", "hooks": [THEIRS]}, *DESIRED["Pre"]],
            "Start": DESIRED["Start"],
        },
        "permissions": {"allow": []},
    }
    assert list(cast("Json", once["hooks"])) == ["Stop", "Pre", "Start"]
    assert settings.merge_events(once, DESIRED) == once


def test_merge_events_replaces_its_own_where_they_stood() -> None:
    config: Json = {
        "hooks": {
            "Pre": [
                {"matcher": "A", "hooks": [THEIRS]},
                {"matcher": "Bash", "hooks": [MINE]},
                {"matcher": "Z", "hooks": [THEIRS]},
            ],
            "Gone": [{"hooks": [MINE]}],
            "Mixed": [{"hooks": [MINE, THEIRS]}],
            "Empty": [],
        }
    }
    assert settings.merge_events(config, DESIRED) == {
        "hooks": {
            "Pre": [
                {"matcher": "A", "hooks": [THEIRS]},
                *DESIRED["Pre"],
                {"matcher": "Z", "hooks": [THEIRS]},
            ],
            "Mixed": [{"hooks": [THEIRS]}],
            "Empty": [],
            "Start": DESIRED["Start"],
        }
    }


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"hooks": []}, "its hooks key is not an object"),
        ({"hooks": {"Pre": {}}}, "its Pre hooks are not a list"),
    ],
)
def test_merge_events_refuses_a_config_it_cannot_read(
    config: Json, message: str
) -> None:
    with pytest.raises(model.InstallError, match=f"^{message}$"):
        settings.merge_events(config, DESIRED)


def test_merge_groups_sets_its_named_groups_and_keeps_the_rest() -> None:
    config: Json = {"mine": {"Pre": []}, "tbhbdev-alpha-identity": {"old": []}}
    merged = settings.merged_config("antigravity", config)
    assert merged == {"mine": {"Pre": []}, **hooks.agy_hooks()}
    assert list(merged)[:2] == ["mine", "tbhbdev-alpha-identity"]


def test_merged_config_per_harness() -> None:
    assert settings.merged_config("claude", {}) == {
        "hooks": hooks.claude_hooks(),
        "env": {"SSH_AUTH_SOCK": ""},
    }
    assert settings.merged_config(
        "claude", {"env": {"A": "1", "SSH_AUTH_SOCK": "x"}}
    ) == {
        "env": {"A": "1", "SSH_AUTH_SOCK": ""},
        "hooks": hooks.claude_hooks(),
    }
    assert settings.merged_config("codex", {}) == {"hooks": hooks.codex_hooks()}


def test_merged_config_refuses_an_env_that_is_not_an_object() -> None:
    with pytest.raises(model.InstallError, match=r"^its env key is not an object$"):
        settings.merged_config("claude", {"env": []})


def test_parse_config() -> None:
    assert settings.parse_config(None) == {}
    assert settings.parse_config(" \t\n") == {}
    assert settings.parse_config('{"a": 1}\n') == {"a": 1}
    with pytest.raises(model.InstallError, match=r"^it is not a JSON object$"):
        settings.parse_config("[]")
    with pytest.raises(model.InstallError, match=r"^it is not valid JSON \("):
        settings.parse_config("{")
