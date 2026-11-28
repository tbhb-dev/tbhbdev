"""Unit tests for fresh GitHub token acquisition and delivery."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import agent_host_example as ex
import pytest

from tbhbdev import secrets
from tbhbdev.alpha import gh_app_api, gh_app_runtime
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


BUILD_REFUSAL = "pull-requests App token unavailable; build refused"
PR_VALUES = {
    "example/pr/client": "example-pr-client-secret",
    "example/pr/pem": "example-pr-pem-secret",
}


@pytest.mark.parametrize(
    ("scenario", "step", "error_name"),
    [
        ("missing_client", "configuration", "Invalid"),
        ("missing_pem", "configuration", "Invalid"),
        ("non_github_host", "configuration", "Invalid"),
        ("client_read", "client id read", "Invalid"),
        ("key_read", "key read", "Invalid"),
        ("mint_gh_app_error", "mint", "GhAppError"),
        ("mint_os_error", "mint", "OSError"),
    ],
)
def test_build_refusal_names_failed_step_and_error_class(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    step: str,
    error_name: str,
) -> None:
    refs = ex.pull_requests_app_settings().credentials[0]
    req = ex.run_request(
        repo="example.com/tbhb-dev/example-repo"
        if scenario == "non_github_host"
        else "tbhb-dev/example-repo"
    )
    if scenario == "missing_client":
        refs = dataclasses.replace(refs, pull_requests_app_client_id=None)
    elif scenario == "missing_pem":
        refs = dataclasses.replace(refs, pull_requests_app_pem=None)

    def fake_read(reference: str) -> str:
        if scenario == "client_read" and reference == "example/pr/client":
            message = "distinctive-client-read-failure"
            raise secrets.SecretsError(message)
        if scenario == "key_read" and reference == "example/pr/pem":
            message = "distinctive-key-read-failure"
            raise secrets.SecretsError(message)
        return PR_VALUES[reference]

    def fake_mint(*_args: object, **_kwargs: object) -> gh_app_api.Minted:
        message = "distinctive-mint-failure"
        if scenario == "mint_gh_app_error":
            raise gh_app_runtime.GhAppError(message)
        raise OSError(message)

    monkeypatch.setattr(secrets, "read", fake_read)
    monkeypatch.setattr(gh_app_api, "mint_token", fake_mint)
    with pytest.raises(Invalid) as caught:
        credentials.github_token(req, refs)
    assert str(caught.value) == f"{BUILD_REFUSAL} (step: {step}; error: {error_name})"
    assert caught.value.__cause__ is None


REVIEW_VALUES = {
    "example/app/client": "fake-review-client-value",
    "example/app/pem": "fake-review-key-value",
}
REVIEW_ERROR_MARKER = "distinctive-review-token-error"
REVIEW_FAILURES = [
    ("missing_configuration", "configuration", "Invalid"),
    ("client_read", "client id read", "Invalid"),
    ("key_read", "key read", "Invalid"),
    ("mint_app_error", "mint", "GhAppError"),
    ("mint_os_error", "mint", "OSError"),
]


def refuse_review_token(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    settings = ex.host_settings()
    references = settings.credentials[0]
    request = ex.run_request(
        profile="review", repo="example-owner/example-repo", commit="a" * 40
    )
    if scenario == "missing_configuration":
        settings = dataclasses.replace(
            settings,
            credentials=(dataclasses.replace(references, reviews_app_pem=None),),
        )

    def fake_read(reference: str) -> str:
        if (scenario, reference) in {
            ("client_read", "example/app/client"),
            ("key_read", "example/app/pem"),
        }:
            raise secrets.SecretsError(REVIEW_ERROR_MARKER)
        return REVIEW_VALUES[reference]

    def fake_mint(*_args: object, **_kwargs: object) -> gh_app_api.Minted:
        if scenario == "mint_app_error":
            raise gh_app_runtime.GhAppError(REVIEW_ERROR_MARKER)
        assert scenario == "mint_os_error"
        raise OSError(REVIEW_ERROR_MARKER)

    monkeypatch.setattr(secrets, "read", fake_read)
    monkeypatch.setattr(gh_app_api, "mint_token", fake_mint)
    credentials.review_post_token(request, settings)


@pytest.mark.parametrize(("scenario", "step", "error_name"), REVIEW_FAILURES)
def test_review_post_refusal_names_failed_step_and_error_class(
    monkeypatch: pytest.MonkeyPatch, scenario: str, step: str, error_name: str
) -> None:
    with pytest.raises(Invalid) as caught:
        refuse_review_token(monkeypatch, scenario)
    assert str(caught.value) == (
        f"reviews App token unavailable (step: {step}; error: {error_name})"
    )


@pytest.mark.parametrize("scenario", [case[0] for case in REVIEW_FAILURES])
def test_review_post_refusal_redacts_secrets_references_and_underlying_message(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    with pytest.raises(Invalid) as caught:
        refuse_review_token(monkeypatch, scenario)
    message = str(caught.value)
    for forbidden in (
        *REVIEW_VALUES.keys(),
        *REVIEW_VALUES.values(),
        REVIEW_ERROR_MARKER,
    ):
        assert forbidden not in message
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "scenario",
    ["client_read", "key_read", "mint_gh_app_error", "mint_os_error"],
)
def test_build_refusal_redacts_secrets_references_and_underlying_message(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    refs = ex.pull_requests_app_settings().credentials[0]
    distinctive = "distinctive-underlying-error-marker"

    def fake_read(reference: str) -> str:
        if (scenario == "client_read" and reference == "example/pr/client") or (
            scenario == "key_read" and reference == "example/pr/pem"
        ):
            raise secrets.SecretsError(distinctive)
        return PR_VALUES[reference]

    def fake_mint(*_args: object, **_kwargs: object) -> gh_app_api.Minted:
        if scenario == "mint_gh_app_error":
            raise gh_app_runtime.GhAppError(distinctive)
        raise OSError(distinctive)

    monkeypatch.setattr(secrets, "read", fake_read)
    monkeypatch.setattr(gh_app_api, "mint_token", fake_mint)
    with pytest.raises(Invalid) as caught:
        credentials.github_token(ex.run_request(), refs)
    text = str(caught.value)
    for forbidden in (
        *PR_VALUES.keys(),
        *PR_VALUES.values(),
        distinctive,
    ):
        assert forbidden not in text
    assert caught.value.__cause__ is None
