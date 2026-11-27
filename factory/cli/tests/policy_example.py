"""Inputs and expected values shared by the run policy tests."""

from __future__ import annotations

import itertools

from agent_example import (
    LINUX,
    MACOS,
    paths,
    settings,
)

from tbhbdev.alpha.agent import host_tools, model
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import (
    Harness,
    Os,
    Profile,
)

HOSTS = {Os.LINUX: LINUX, Os.MACOS: MACOS}
ACCOUNTS = ["agent", "ops"]
COMBINATIONS = list(
    itertools.product(Os, Profile, Harness, ACCOUNTS),
)
IDS = [f"{o.value}-{p.value}-{h.value}-{a}" for o, p, h, a in COMBINATIONS]
PATH = {
    Os.LINUX: "{home}/.local/bin:/usr/local/bin:/usr/bin",
    Os.MACOS: "{home}/.local/bin:/opt/example/bin:/usr/bin",
}


def made(  # noqa: PLR0913 -- one value per fact the policy depends on
    os: Os,
    profile: Profile,
    harness: Harness,
    account: str,
    *,
    unsandboxed: bool = False,
    config: model.Settings | None = None,
) -> model.Policy:
    return policy.build(
        config or settings(),
        HOSTS[os],
        harness=harness,
        profile=profile,
        account=account,
        unsandboxed=unsandboxed,
        paths=paths(account),
    )


def guard_hooks(program: str) -> dict[str, object]:
    handler = {
        "type": "command",
        "command": f"{program} alpha guard-agent-model",
        "onFailure": "block",
    }
    return {"PreToolUse": [{"matcher": "Agent", "hooks": [handler]}]}


def claude_settings_of(result: model.Policy) -> dict[str, object]:
    detail = result.detail
    assert isinstance(detail, model.ClaudePolicy)
    return detail.settings


def strings(node: object) -> list[str]:
    assert isinstance(node, list)
    items: list[object] = list(node)
    assert all(isinstance(item, str) for item in items)
    return [str(item) for item in items]


def dig(node: object, *keys: str) -> object:
    for key in keys:
        assert isinstance(node, dict)
        node = node[key]
    return node


def env_of(os: Os, harness: Harness, account: str) -> dict[str, str]:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    env = {
        "PATH": ":".join(host_tools.search_path(settings(), os)),
        "HOME": home,
        "USER": account,
        "LOGNAME": account,
        "SHELL": "/bin/bash" if os is Os.LINUX else "/bin/zsh",
        "LANG": "C.UTF-8" if os is Os.LINUX else "en_US.UTF-8",
        "TMPDIR": tmp,
        "CLAUDE_CODE_TMPDIR": tmp,
        "TMPPREFIX": f"{tmp}/zsh",
        "GH_CONFIG_DIR": f"{tmp}/gh",
        "DOCKER_CONFIG": f"{tmp}/docker",
        "PYTEST_DEBUG_TEMPROOT": tmp,
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "4",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "7",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "safe.bareRepository",
        "GIT_CONFIG_VALUE_1": "explicit",
        "GIT_CONFIG_KEY_2": "diff.ignoreSubmodules",
        "GIT_CONFIG_VALUE_2": "all",
        "GIT_CONFIG_KEY_3": "status.submoduleSummary",
        "GIT_CONFIG_VALUE_3": "false",
        "GIT_CONFIG_KEY_4": "protocol.ext.allow",
        "GIT_CONFIG_VALUE_4": "never",
        "GIT_CONFIG_KEY_5": "protocol.file.allow",
        "GIT_CONFIG_VALUE_5": "user",
        "GIT_CONFIG_KEY_6": "diff.external",
        "GIT_CONFIG_VALUE_6": "",
    }
    if harness is Harness.CODEX:
        env["CODEX_HOME"] = f"{tmp}/codex"
    return env
