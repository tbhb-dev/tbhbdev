"""agy run policy grants."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import (
    SANDBOX,
    settings,
)
from policy_example import (
    ACCOUNTS,
    made,
)

from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.model import (
    Harness,
    Os,
    Profile,
)


def agy_expected(profile: Profile, account: str) -> tuple[str, ...]:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    handoff = (f"read_file({tmp}/gh)", f"read_file({tmp}/docker)")
    if profile is Profile.REVIEW:
        return (
            f"read_file({home}/.local/share/example-gh)",
            f"read_file({home}/.local/share/example-mise)",
            *handoff,
            f"write_file({home}/.local/state/example-audit)",
        )
    worktree = f"{home}/Code/example.com/example-org/example-repo"
    return (
        f"read_file({home}/.local/share/example-tools)",
        *handoff,
        f"write_file({worktree})",
        f"write_file({tmp})",
        f"write_file({home}/.local/state/mise)",
        f"write_file({home}/.local/share/mise)",
    )


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_agy_grants(os: Os, profile: Profile, account: str) -> None:
    result = made(os, profile, Harness.AGY, account)
    assert result.detail == model.AgyPolicy(agy_expected(profile, account))


@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("reason", [None, "isolated test machine"])
def test_agy_needs_no_named_opt_out(profile: Profile, reason: str | None) -> None:
    configured = settings(
        sandbox=dataclasses.replace(SANDBOX, agy_unsandboxed_opt_out=reason)
    )
    assert configured.sandbox.agy_unsandboxed_opt_out == reason
    assert isinstance(
        made(Os.LINUX, profile, Harness.AGY, "agent", config=configured).detail,
        model.AgyPolicy,
    )
