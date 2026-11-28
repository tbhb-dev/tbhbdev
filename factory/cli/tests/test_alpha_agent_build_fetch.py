"""A build run fetches with the pull-requests App and never reads the reviews App."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agent_host_example import (
    build_settings,
    pull_requests_app_settings,
    run_request,
)

from tbhbdev import secrets
from tbhbdev.alpha import gh_app_api, gh_app_runtime
from tbhbdev.alpha.agent import credentials
from tbhbdev.alpha.agent.model import Credentials, Invalid

if TYPE_CHECKING:
    from pathlib import Path

PR_APP_STUB = "stub-pr-app-minted"
REVIEWS_APP_STUB = "stub-reviews-app-minted"
REVIEW_SCOPES = {"contents", "pull_requests", "checks", "statuses", "actions"}
VALUES = {
    "example/gh/value": "stub-gh",
    "example/docker/value": "{}",
    "example/signing/key": "stub-signing",
    "example/app/client": "stub-reviews-client",
    "example/app/pem": "stub-reviews-pem",
    "example/pr/client": "stub-pr-client",
    "example/pr/pem": "stub-pr-pem",
}


class RecordingStore:
    """A fake secrets store that records every reference it is asked to read."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def read(self, reference: str) -> str:
        self.reads.append(reference)
        return VALUES[reference]


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch) -> RecordingStore:
    recorder = RecordingStore()
    monkeypatch.setattr(secrets, "read", recorder.read)
    return recorder


READ_ONLY_CONTENTS = {"contents": "read"}
REVIEWS_REFERENCES = ("example/app/client", "example/app/pem")


@pytest.fixture
def split_apps(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Mint stub that records each request and answers per calling App."""
    seen: list[dict[str, object]] = []

    def mint(_env: gh_app_runtime.Env, **keywords: object) -> gh_app_api.Minted:
        seen.append(keywords)
        reviews = keywords["app_id"] == VALUES["example/app/client"]
        return gh_app_api.Minted(REVIEWS_APP_STUB if reviews else PR_APP_STUB, 2000)

    monkeypatch.setattr(gh_app_api, "mint_token", mint)
    return seen


def test_build_preparation_never_reads_a_reviews_app_reference(
    tmp_path: Path, store: RecordingStore, split_apps: list[dict[str, object]]
) -> None:
    settings = pull_requests_app_settings()
    request = run_request()
    assert credentials.fetch_token(request, settings) == PR_APP_STUB
    credentials.deliver(request, settings, tmp_path)
    assert not set(store.reads) & set(REVIEWS_REFERENCES)
    assert {"example/pr/client", "example/pr/pem"} <= set(store.reads)
    assert [keywords["app_id"] for keywords in split_apps] == ["stub-pr-client"] * 2


def test_build_fetch_token_is_read_only_for_the_one_repository(
    split_apps: list[dict[str, object]],
) -> None:
    request = run_request()
    credentials.fetch_token(request, pull_requests_app_settings())
    assert [keywords["permissions"] for keywords in split_apps] == [READ_ONLY_CONTENTS]
    assert [keywords["repository"] for keywords in split_apps] == [request.repo]


def test_review_fetch_still_uses_the_reviews_app(
    split_apps: list[dict[str, object]],
) -> None:
    request = run_request(profile="review", commit="0" * 40)
    assert (
        credentials.fetch_token(request, pull_requests_app_settings())
        == REVIEWS_APP_STUB
    )
    assert [keywords["app_id"] for keywords in split_apps] == ["stub-reviews-client"]
    assert [keywords["permissions"] for keywords in split_apps] == [
        dict.fromkeys(REVIEW_SCOPES, "read")
    ]


@pytest.mark.parametrize(
    "app_references",
    [
        {"pull_requests_app_client_id": "example/pr/client"},
        {"pull_requests_app_pem": "example/pr/pem"},
        {"reviews_app_client_id": "example/app/client"},
        {"reviews_app_pem": "example/app/pem"},
    ],
)
def test_github_token_refuses_app_references_beside_a_gh_reference(
    store: RecordingStore,
    split_apps: list[dict[str, object]],
    app_references: dict[str, str],
) -> None:
    references = Credentials(
        "agent",
        "example/gh/value",
        None,
        app_references.get("reviews_app_client_id"),
        app_references.get("reviews_app_pem"),
        "example/signing/key",
        app_references.get("pull_requests_app_client_id"),
        app_references.get("pull_requests_app_pem"),
    )
    with pytest.raises(Invalid) as caught:
        credentials.github_token(run_request(), references)
    assert str(caught.value) == (
        "gh reference conflicts with App references; configure App tokens only"
    )
    assert store.reads == []
    assert split_apps == []


def test_github_token_with_only_a_gh_reference_reads_it(
    store: RecordingStore,
) -> None:
    references = build_settings().credentials[0]
    assert (
        credentials.github_token(run_request(), references)
        == (VALUES["example/gh/value"])
    )
    assert store.reads == ["example/gh/value"]
