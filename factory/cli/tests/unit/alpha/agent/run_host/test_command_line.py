"""Build harness commands from literal run inputs."""

import dataclasses
import itertools
import json
import tomllib

import pytest
from agent_example import LINUX, paths, settings
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile
from tbhbdev.alpha.agent.run_host import command_line, environment


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
def test_full_access_arguments(harness: Harness, profile: Profile) -> None:
    value = request.parse(
        BUILD
        | {
            "harness": harness.value,
            "profile": profile.value,
            "commit": "a" * 40 if profile is Profile.REVIEW else None,
            "claude_profile": "primary" if harness is Harness.CLAUDE else None,
            "reason": "review" if harness is Harness.CLAUDE else None,
            "timeout": 900 if harness is Harness.AGY else None,
            "unsandboxed": True,
        }
    )
    files = paths("agent")
    files = dataclasses.replace(
        files, worktree="/example/repo.with.dots", signing_socket="/example/sign.sock"
    )
    policy = environment.build(settings(), LINUX, value, files, "/example/bin")
    command = command_line.harness(
        value,
        policy,
        worktree=files.worktree,
        executable="/example/tool",
        final_message="/example/final",
        stop_hooks={"Stop": []},
        agy_project="/example/project",
        agy_brief="--literal brief",
        schema_file="/example/schema",
        schema_text='{"type":"object"}',
    )
    assert command[0] == "/example/tool"
    if harness is Harness.CODEX:
        assert command[1] == "exec"
        assert command[command.index("-s") + 1] == "danger-full-access"
        assert ("--ephemeral" in command) is (profile is Profile.REVIEW)
        assert command[-5:] == (
            "--output-schema",
            "/example/schema",
            "--output-last-message",
            "/example/final",
            "-",
        )
        overrides = "\n".join(
            item for flag, item in itertools.pairwise(command) if flag == "-c"
        )
        config = tomllib.loads(overrides)
        assert config["projects"] == {files.worktree: {"trust_level": "untrusted"}}
        assert config["features"]["network_proxy"] == {"enabled": False}
        assert (
            config["agents"]["default_subagent_model"]
            == settings().codex.subagent_model
        )
        assert config["shell_environment_policy"]["exclude"] == ["GIT_*"]
        # Bazel's Go fetch rule rejects a count of 0, so no count is set at all.
        assert not any(
            name.startswith("GIT_")
            for name in config["shell_environment_policy"]["set"]
        )
        assert (
            config["shell_environment_policy"]["set"]["SSH_AUTH_SOCK"]
            == files.signing_socket
        )
    elif harness is Harness.CLAUDE:
        assert command[1] == "-p"
        assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
        assert command[command.index("--setting-sources") + 1] == ""
        config = json.loads(command[command.index("--settings") + 1])
        assert config["sandbox"] == {"enabled": False}
        assert config["env"]["CLAUDE_CODE_FORK_SUBAGENT"] == "0"
        assert set(config["hooks"]) == {"PreToolUse", "Stop"}
        assert command[-4:] == (
            "--output-format",
            "json",
            "--json-schema",
            '{"type":"object"}',
        )
    else:
        assert "--dangerously-skip-permissions" in command
        assert ("--mode" in command) is (profile is Profile.REVIEW)
        assert command[-4:-1] == ("--print-timeout", "900s", "--print")
        assert command[-1].endswith("\n--literal brief")


def test_agy_project_uses_the_supplied_run_facts() -> None:
    assert json.loads(command_line.agy_project("sample", "/repo", (), 600)) == {
        "name": "agent-sample",
        "path": "/repo",
        "permissions": [],
        "timeout": 600,
    }


@pytest.mark.parametrize(
    ("project", "brief", "timeout", "error"),
    [
        (None, "example", 60, "requires agy_project"),
        ("/example/project", None, 60, "requires agy_brief"),
        ("/example/project", "example", None, "requires a timeout"),
    ],
)
def test_agy_command_refuses_missing_inputs(
    project: str | None, brief: str | None, timeout: int | None, error: str
) -> None:
    value = request.parse(BUILD | {"harness": "agy", "timeout": 60})
    files = paths("agent")
    policy = environment.build(settings(), LINUX, value, files, "/example/bin")
    with pytest.raises(Invalid, match=error):
        command_line.harness(
            dataclasses.replace(value, timeout=timeout),
            policy,
            worktree=files.worktree,
            executable="/example/tool",
            final_message="/example/final",
            stop_hooks={},
            agy_project=project,
            agy_brief=brief,
        )


@pytest.mark.parametrize(
    ("profile", "harness"),
    [(Profile.REVIEW, Harness.CODEX), (Profile.BUILD, Harness.CLAUDE)],
)
def test_command_refuses_unmatched_policy(
    profile: Profile,
    harness: Harness,
) -> None:
    value = request.parse(BUILD)
    files = paths("agent")
    policy = environment.build(settings(), LINUX, value, files, "/example/bin")
    with pytest.raises(
        Invalid, match="policy was built for another harness or profile"
    ):
        command_line.harness(
            value,
            dataclasses.replace(policy, profile=profile, harness=harness),
            worktree=files.worktree,
            executable="/example/tool",
            final_message="/example/final",
            stop_hooks={},
        )
