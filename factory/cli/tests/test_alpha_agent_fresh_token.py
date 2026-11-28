"""A run older than an hour publishes with a freshly minted credential."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from agent_host_example import host_settings, run_request

from tbhbdev.alpha.agent import credentials
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Credentials

HOUR = 3600
PUSH = {"contents": "write", "pull_requests": "write", "workflows": "write"}


def app_references() -> Credentials:
    return dataclasses.replace(
        host_settings().credentials[0],
        gh=None,
        pull_requests_app_client_id="example/app/client",
        pull_requests_app_pem="example/app/pem",
    )


def test_expired_stored_token_is_replaced_by_a_fresh_mint() -> None:
    request = run_request()
    stored = credentials.StoredToken("expired", minted_at=1000.0)
    minted: list[tuple[str, dict[str, str]]] = []

    def source(_references: Credentials, repo: str, permissions: dict[str, str]) -> str:
        minted.append((repo, permissions))
        return "fresh"

    got = credentials.fresh_token(
        request,
        app_references(),
        stored,
        clock=lambda: 1000.0 + 2 * HOUR,
        source=source,
    )
    assert got == "fresh"
    assert minted == [(request.repo, PUSH)]


def test_young_stored_token_is_kept() -> None:
    def source(*_args: object) -> str:
        pytest.fail("a young token needs no mint")

    stored = credentials.StoredToken("young", minted_at=1000.0)
    got = credentials.fresh_token(
        run_request(), app_references(), stored, clock=lambda: 1060.0, source=source
    )
    assert got == "young"


def test_without_app_references_the_static_credential_is_read() -> None:
    references = dataclasses.replace(app_references(), pull_requests_app_client_id=None)
    references = dataclasses.replace(references, pull_requests_app_pem=None)
    got = credentials.fresh_token(run_request(), references, None, clock=lambda: 0.0)
    assert got == credentials.github_token(run_request(), references)


def test_refusal_names_the_cause() -> None:
    def source(*_args: object) -> str:
        message = "app unavailable"
        raise Invalid(message)

    with pytest.raises(Invalid, match="could not get a fresh GitHub credential"):
        credentials.fresh_token(
            run_request(), app_references(), None, clock=lambda: 0.0, source=source
        )
