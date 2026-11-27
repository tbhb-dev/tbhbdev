"""Exercise coordinator reviews through fake tools and run records."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import Mock

import pytest
from agent_cli_example import run_agent
from driver_scenarios import (
    COMMIT,
    CONFIG,
    arguments,
    launch,
)

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.driver import (
    metadata,
    reviews,
)
from tbhbdev.alpha.agent.model import Invalid, Profile, RunRequest

if TYPE_CHECKING:
    from agent_driver_example import Remote

    from tbhbdev.alpha.agent.driver.decisions.metadata import Run


OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"


def recorded_review(remote: Remote, *extra: str) -> Run:
    """Launch `sample`, then return a copy named `second` to check against it."""
    assert launch(remote, "--profile", "review", "--ref", COMMIT, *extra) == 0
    second = metadata.load(CONFIG, "sample")
    second.name = "second"
    return second


def test_a_second_live_review_of_the_same_commit_is_refused(remote: Remote) -> None:
    second = recorded_review(remote)
    with pytest.raises(Invalid) as info:
        reviews.refuse_live_duplicate(CONFIG, second)
    assert str(info.value) == (
        f"second: run sample already reviews example-org/example-repo@{COMMIT} "
        "with the review profile and hasn't ended. "
        "Check `tbhbdev alpha agent status sample` and use "
        "`tbhbdev alpha agent clean sample` once it ends. "
        "For concurrent reviewers, launch both with distinct --variant values"
    )


def test_a_review_may_start_when_the_first_has_ended(remote: Remote) -> None:
    second = recorded_review(remote)
    remote.file(".phase", "finished\n")
    remote.file(".rc", "0\n")
    reviews.refuse_live_duplicate(CONFIG, second)


def test_an_unreachable_host_still_counts_the_first_run_as_live(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = recorded_review(remote)

    def unreachable(*_args: object, **_kwargs: object) -> transport.Reading:
        message = "example-linux: transport failed"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "read", unreachable)
    with pytest.raises(Invalid, match="hasn't ended"):
        reviews.refuse_live_duplicate(CONFIG, second)


def age_first_run(**request: object) -> None:
    """Rewrite the recorded `sample` run as started in 2020."""
    path = metadata.metadata_path("sample")
    record = json.loads(path.read_text())
    record["started"] = "2020-01-01T00:00:00+00:00"
    record.update(request)
    path.write_text(json.dumps(record))


def test_a_first_run_past_its_time_limit_no_longer_blocks(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = recorded_review(remote)
    age_first_run(timeout=60)

    def no_read(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("past the limit, no read")

    monkeypatch.setattr(transport, "read", no_read)
    reviews.refuse_live_duplicate(CONFIG, second)


def test_a_record_that_disappears_during_the_scan_is_skipped(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = recorded_review(remote)
    monkeypatch.setattr(reviews, "load", Mock(side_effect=FileNotFoundError))
    reviews.refuse_live_duplicate(CONFIG, second)


@pytest.mark.parametrize("change", ["commit", "profile"])
def test_another_commit_or_profile_does_not_count_as_a_duplicate(
    remote: Remote, change: str
) -> None:
    second = recorded_review(remote)
    (metadata.directory() / "broken.json").write_text("{")
    changes: dict[str, RunRequest] = {
        "commit": dataclasses.replace(second.request, commit=OTHER_COMMIT),
        "profile": dataclasses.replace(second.request, profile=Profile.BUILD),
    }
    second.request = changes[change]
    reviews.refuse_live_duplicate(CONFIG, second)


def test_two_declared_variants_may_review_one_commit(remote: Remote) -> None:
    second = recorded_review(remote, "--variant", "a")
    second.request = dataclasses.replace(second.request, variant="b")
    reviews.refuse_live_duplicate(CONFIG, second)


def test_a_fifth_live_review_of_the_same_commit_is_refused(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    review = recorded_review(remote, "--variant", "one")
    live = transport.Reading("running", None, launched=True, session=True, text="")
    monkeypatch.setattr(transport, "read", Mock(return_value=live))
    for variant in ("two", "three", "four"):
        review.name = f"review-{variant}"
        review.request = dataclasses.replace(review.request, variant=variant)
        reviews.refuse_live_duplicate(CONFIG, review)
        metadata.save(review)

    stream = Mock(side_effect=AssertionError("started a fifth review"))
    monkeypatch.setattr(transport, "stream", stream)
    argv = arguments(
        remote, "--profile", "review", "--ref", COMMIT, "--variant", "five"
    )
    argv[-2] = "review-five"
    assert run_agent(argv) == 1
    assert "already has 4 live reviews" in capsys.readouterr().err
    assert not metadata.metadata_path("review-five").exists()
    stream.assert_not_called()


@pytest.mark.parametrize(
    ("first", "later"),
    [
        (None, "b"),
        ("a", None),
        ("a", "a"),
        (None, None),
    ],
)
def test_a_run_without_a_variant_or_with_the_same_one_is_a_duplicate(
    remote: Remote, first: str | None, later: str | None
) -> None:
    second = recorded_review(remote, *(("--variant", first) if first else ()))
    second.request = dataclasses.replace(second.request, variant=later)
    with pytest.raises(Invalid, match="hasn't ended"):
        reviews.refuse_live_duplicate(CONFIG, second)


@pytest.mark.parametrize("field", ["role", "class_"])
def test_a_different_role_or_class_alone_is_still_a_duplicate(
    remote: Remote, field: str
) -> None:
    second = recorded_review(remote)
    changes: dict[str, RunRequest] = {
        "role": dataclasses.replace(second.request, role="b"),
        "class_": dataclasses.replace(second.request, class_="b"),
    }
    second.request = changes[field]
    with pytest.raises(Invalid, match="hasn't ended"):
        reviews.refuse_live_duplicate(CONFIG, second)
