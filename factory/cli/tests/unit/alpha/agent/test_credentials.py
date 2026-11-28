"""Unit tests for fresh GitHub token acquisition and delivery."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
import yaml
from agent_example import settings as example_settings

from tbhbdev import secrets
from tbhbdev.alpha.agent import credentials
from tbhbdev.alpha.agent.model import (
    Credentials,
    Harness,
    Invalid,
    Profile,
    RunKind,
    RunRequest,
)

if TYPE_CHECKING:
    from pathlib import Path

HOUR = 3600
PUSH = {"contents": "write", "pull_requests": "write", "workflows": "write"}


def build_request(profile: Profile = Profile.BUILD) -> RunRequest:
    commit = "0" * 40 if profile is Profile.REVIEW else None
    return RunRequest(
        RunKind.EXEC, "tbhb-dev/example-repo", commit, Harness.CODEX,
        "gpt-5.4", "high", profile, None, None, "agent", 900,
        None, None, None, unsandboxed=True, authorized_by=None,
        startup_deadline_seconds=60,
    )  # fmt: skip


def app_references() -> Credentials:
    return Credentials(
        "agent", None, None, "example/reviews/client", "example/reviews/pem",
        "example/signing/key", "example/app/client", "example/app/pem",
    )  # fmt: skip


def test_fresh_token_replaces_expired_keeps_young_and_reads_static(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = build_request()
    minted: list[tuple[str, dict[str, str]]] = []

    def source(_refs: Credentials, repo: str, permissions: dict[str, str]) -> str:
        minted.append((repo, permissions))
        return "fresh"

    stored = credentials.StoredToken("expired", minted_at=1000.0)
    got = credentials.fresh_token(
        request,
        app_references(),
        stored,
        clock=lambda: 1000.0 + 2 * HOUR,
        source=source,
    )
    assert got == "fresh"
    assert minted == [(request.repo, PUSH)]
    kept = credentials.fresh_token(
        request, app_references(), stored, clock=lambda: 1060.0, source=source
    )
    assert kept == "expired"
    assert len(minted) == 1

    def read_static(ref: str) -> str:
        return "static-gh-token" if ref == "example/gh/val" else ""

    monkeypatch.setattr(secrets, "read", read_static)
    static = Credentials("agent", "example/gh/val", None, None, None, "signing")
    assert credentials.fresh_token(request, static) == "static-gh-token"

    def fail(*_args: object) -> str:
        message = "app unavailable"
        raise Invalid(message)

    with pytest.raises(Invalid, match="could not get a fresh GitHub credential"):
        credentials.fresh_token(request, app_references(), source=fail)


def test_deliver_records_stored_token_only_for_pull_requests_app_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets, "read", lambda ref: f"val:{ref}")
    monkeypatch.setattr(credentials, "pull_requests_token", lambda *_a: "launch-app")
    monkeypatch.setattr(credentials, "installation_token", lambda *_a: "review-app")
    app_cfg = dataclasses.replace(example_settings(), credentials=(app_references(),))
    got = credentials.deliver(
        build_request(), app_cfg, tmp_path / "app", clock=lambda: 1000.0
    )
    assert got.gh_token == credentials.StoredToken("launch-app", 1000.0)
    assert "launch-app" not in repr(got)
    hosts = yaml.safe_load((tmp_path / "app" / "gh" / "hosts.yml").read_text())
    expected = {"github.com": {"oauth_token": "launch-app", "git_protocol": "https"}}
    assert hosts == expected
    static = Credentials("agent", "example/gh/val", None, None, None, "signing")
    static_cfg = dataclasses.replace(example_settings(), credentials=(static,))
    static_out = credentials.deliver(build_request(), static_cfg, tmp_path / "s")
    review_out = credentials.deliver(
        build_request(Profile.REVIEW), app_cfg, tmp_path / "r"
    )
    assert static_out.gh_token is None
    assert review_out.gh_token is None
