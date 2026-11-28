"""Unit tests for fresh GitHub token acquisition and delivery."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agent_host_example as ex
import pytest

from tbhbdev import secrets
from tbhbdev.alpha.agent import credentials
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path


def test_fresh_token_and_deliver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req, app_cfg = ex.run_request(), ex.pull_requests_app_settings()
    refs, ft, dlv = app_cfg.credentials[0], credentials.fresh_token, credentials.deliver
    minted: list[tuple[str, dict[str, str]]] = []
    fail = [False]

    def mint(_refs: object, repo: str, perms: dict[str, str]) -> str:
        if fail[0]:
            raise Invalid(credentials.FRESH_REFUSAL)
        minted.append((repo, perms))
        return "fresh"

    monkeypatch.setattr(credentials, "pull_requests_token", mint)
    tok = credentials.StoredToken("old", 1000.0)
    assert ft(req, refs, tok, clock=lambda: 8200.0) == "fresh"
    assert minted == [(req.repo, credentials.PUSH_PERMISSIONS)]
    assert (ft(req, refs, tok, clock=lambda: 1060.0), len(minted)) == ("old", 1)
    monkeypatch.setattr(secrets, "read", ex.constant("static-gh"))
    assert ft(req, ex.build_settings().credentials[0]) == "static-gh"
    monkeypatch.setattr(credentials.time, "time", ex.constant(1000.0))
    monkeypatch.setattr(credentials, "installation_token", ex.constant("review"))
    got = dlv(req, app_cfg, tmp_path / "app")
    assert got.gh_token == credentials.StoredToken("fresh", 1000.0)
    assert "fresh" not in repr(got)
    want = "github.com:\n  git_protocol: https\n  oauth_token: fresh\n"
    assert (tmp_path / "app/gh/hosts.yml").read_text() == want
    assert dlv(req, ex.build_settings(), tmp_path / "s").gh_token is None
    rev = ex.run_request(profile="review", commit="0" * 40)
    assert dlv(rev, app_cfg, tmp_path / "r").gh_token is None
    fail[0] = True
    with pytest.raises(Invalid, match=credentials.FRESH_REFUSAL):
        ft(req, refs)
