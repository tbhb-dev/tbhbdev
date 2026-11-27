"""Verify an origin branch contains the build's recorded commit."""

from unittest.mock import Mock

import httpx2
import pytest

from tbhbdev.alpha.agent import push, run_host


@pytest.mark.parametrize(
    "case",
    [
        (None, 200, {}, None),
        ("", 200, {}, False),
        ("a" * 40, 200, {}, True),
        ("b" * 40, 200, {"status": "ahead"}, True),
        ("b" * 40, 200, {"status": "diverged"}, False),
        ("b" * 40, 200, [], None),
        ("b" * 40, 404, {}, False),
        ("b" * 40, 403, {}, None),
    ],
)
def test_origin_contains_recorded_commit(
    monkeypatch: pytest.MonkeyPatch, case: tuple[str | None, int, object, bool | None]
) -> None:
    tip, status, body, expected = case
    run = Mock(spec=run_host.Run)
    response = httpx2.Response(
        status, json=body, request=httpx2.Request("GET", "https://example.test")
    )
    lookup = Mock(return_value=response)
    monkeypatch.setattr(run_host, "origin_tip", Mock(return_value=tip))
    monkeypatch.setattr(push, "github_get", lookup)
    commit = "a" * 40
    assert run_host.origin_contains(run, commit) is expected
    if tip and tip != commit:
        lookup.assert_called_once_with(run, f"compare/{commit}...{tip}?per_page=1")
    else:
        lookup.assert_not_called()
