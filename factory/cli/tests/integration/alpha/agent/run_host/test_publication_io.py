"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import ANY, Mock

import httpx2
import pytest
from agent_host_example import (
    constant,
    git,
    make_run,
    repository,
)
from run_host_fixtures import (
    HOST_FIXTURES,
    PUSH_RUN,
    HostRuntime,
    preparation,
)

from tbhbdev.alpha.agent import (
    credentials,
    process_group,
    push,
    review_post,
    runfiles,
)
from tbhbdev.alpha.agent.model import (
    Profile,
)
from tbhbdev.alpha.agent.run_host import execution as host_execution
from tbhbdev.alpha.agent.run_host import harness as host_harness
from tbhbdev.alpha.agent.run_host import preflight as host_preflight
from tbhbdev.alpha.agent.run_host import publication as host_publication
from tbhbdev.alpha.agent.run_host import republish as host_republish
from tbhbdev.alpha.agent.run_host import state

__all__ = ["pytestmark"]

pytestmark = HOST_FIXTURES


@pytest.mark.parametrize(
    ("profile", "rc", "preflight_only", "uncertain", "posted", "should_post"),
    [
        (Profile.REVIEW, 0, False, False, True, True),
        (Profile.REVIEW, 0, False, False, False, True),
        (Profile.REVIEW, 7, False, False, True, False),
        (Profile.REVIEW, 0, True, False, True, False),
        (Profile.REVIEW, 0, False, True, True, False),
        (Profile.BUILD, 0, False, False, True, False),
    ],
)
def test_execute_posts_only_after_a_successful_review(  # noqa: PLR0913
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    rc: int,
    *,
    preflight_only: bool,
    uncertain: bool,
    posted: bool,
    should_post: bool,
) -> None:
    commit = "a" * 40 if profile is Profile.REVIEW else None
    run = make_run(tmp_path, profile=profile.value, commit=commit)
    prepared = preparation(run)
    monkeypatch.setattr(host_preflight, "preflight", Mock())
    monkeypatch.setattr(process_group, "prepare", Mock(return_value=prepared))

    def harness(*_args: object, **_kwargs: object) -> int:
        run.files.uncertain = uncertain
        return rc

    finish_harness = Mock(side_effect=harness)
    monkeypatch.setattr(host_harness, "run_harness", finish_harness)

    def record_review(actual: state.Run, ready: state.Prepared) -> bool:
        finish_harness.assert_called_once()
        assert actual is run
        assert ready is prepared
        assert Path(ready.paths.worktree).is_dir()
        assert not run.files.path("rc").exists()
        return posted

    post = Mock(side_effect=record_review)
    monkeypatch.setattr(review_post, "posted", post)
    expected = 10 if uncertain or (should_post and not posted) else rc
    assert (
        host_execution.execute(run, "example", preflight_only=preflight_only)
        == expected
    )
    assert post.call_count == int(should_post)
    assert run.files.path("rc").read_text() == f"{expected}\n"
    if uncertain:
        assert (
            "review not posted: harness shutdown unconfirmed"
            in run.files.path("log").read_text()
        )


@pytest.mark.parametrize(
    ("failure", "rc", "log"),
    [
        (None, 10, "review not posted: the draft frontmatter is nested too deeply\n"),
        (RuntimeError("private"), 10, "review not posted: RuntimeError\n"),
        (TimeoutError("private"), 10, "review not posted: TimeoutError\n"),
        (
            subprocess.TimeoutExpired(["private"], 20),
            10,
            "review not posted: TimeoutExpired\n",
        ),
        (state.RunInterruptedError(), 143, None),
    ],
    ids=["nested-yaml", "unexpected", "timeout", "subprocess-timeout", "signal"],
)
def test_review_post_failure_sets_the_completion_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | None,
    rc: int,
    log: str | None,
) -> None:
    run = make_run(tmp_path, profile=Profile.REVIEW.value, commit="a" * 40)
    prepared = preparation(run)
    monkeypatch.setattr(host_preflight, "preflight", Mock())
    monkeypatch.setattr(process_group, "prepare", Mock(return_value=prepared))
    monkeypatch.setattr(host_harness, "run_harness", Mock(return_value=0))
    run.files.write("out.final", f"VERDICT: GO {run.request.commit}\n")
    if failure is None or isinstance(failure, TimeoutError):
        directory = Path(prepared.paths.worktree) / ".git" / "tbhb-alpha"
        directory.mkdir(parents=True)
        draft = (
            f"---\npr: 7\ncommit_id: '{run.request.commit}'\nevent: COMMENT\n---\n"
            "## Summary\n\nThe change preserves existing behavior.\n\n"
            "## Verification\n\n- Read the diff.\n"
        )
        if failure is None:
            nested_yaml = "[" * 1000 + "]" * 1000
            draft = draft.replace("pr: 7", f"pr: {nested_yaml}")
        (directory / "review-7.md").write_text(draft)
        monkeypatch.setattr(
            credentials,
            "review_post_token",
            Mock(return_value="fake", side_effect=failure),
        )
    else:
        monkeypatch.setattr(review_post, "posted", Mock(side_effect=failure))

    assert host_execution.execute(run, "example") == rc
    assert run.files.path("rc").read_text() == f"{rc}\n"
    assert run.files.path("phase").read_text() == "ended"
    assert runfiles.latest(run.files.root)[run.files.name]["rc"] == rc
    assert not run.files.path("review").exists()
    if log is None:
        assert not run.files.path("log").exists()
    else:
        assert run.files.path("log").read_text() == log


@pytest.mark.parametrize(
    ("expect_commit", "harness_result", "published", "start_ref", "expected"),
    [
        (True, (0, None), None, None, 6),
        (True, (0, "a" * 40), True, None, 0),
        (True, (0, "a" * 40), False, None, 9),
        (True, (0, "a" * 40), None, None, 9),
        (True, (0, "a" * 40), True, "refs/heads/topic", 0),
        (True, (0, "a" * 40), False, "refs/heads/topic", 9),
        (True, (0, "a" * 40), None, "refs/heads/topic", 9),
        (True, (4, None), None, None, 4),
        (True, (4, "a" * 40), False, "refs/heads/topic", 4),
        (False, (0, None), None, None, 0),
    ],
)
def test_execute_checks_required_build_results(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expect_commit: bool,
    harness_result: tuple[int, str | None],
    published: bool | None,
    start_ref: str | None,
    expected: int,
) -> None:
    run = make_run(tmp_path, expect_commit=expect_commit)
    run.request = dataclasses.replace(run.request, start_ref=start_ref)
    prepared = dataclasses.replace(preparation(run), auth_before="example-before")
    HostRuntime(run, prepared, None).patch(monkeypatch)
    origin_contains = Mock(return_value=published)
    monkeypatch.setattr(host_publication, "origin_contains", origin_contains)
    republish = Mock(wraps=host_republish.republish)
    monkeypatch.setattr(host_republish, "republish", republish)

    def publish(*_args: object) -> int:
        origin_contains.assert_not_called()
        status, recorded_head = harness_result
        if recorded_head is not None:
            run.files.write("head", recorded_head)
        return status

    monkeypatch.setattr(push, "run", publish)
    assert host_execution.execute(run, "example") == expected
    assert run.files.path("rc").read_text() == f"{expected}\n"
    assert runfiles.latest(run.files.root)["example"]["rc"] == expected
    log = run.files.path("log")
    message = "no new commit: the build required one (--expect-commit)\n"
    assert (log.exists() and message in log.read_text().splitlines(keepends=True)) is (
        expected == 6
    )
    branch = start_ref.removeprefix("refs/heads/") if start_ref else "agent/example"
    if expected == 9:
        reason = "cannot verify" if published is None else "build commit missing from"
        assert f"push failed: {reason} {branch} on origin" in log.read_text()
    assert run.files.path("branch").exists() is bool(published)
    should_republish = (
        expect_commit
        and harness_result[0] == 0
        and harness_result[1] is not None
        and published is False
        and start_ref is not None
    )
    assert republish.call_count == int(should_republish)


BRANCH_REF = "refs/heads/agent/example"


@pytest.mark.parametrize(
    ("result", "token", "tip"),
    [
        ((200, {"ref": BRANCH_REF, "object": {"sha": "a" * 40}}), "fake", "a" * 40),
        ((200, {"ref": BRANCH_REF, "object": {"sha": "invalid"}}), None, None),
        ((200, {"ref": BRANCH_REF}), None, None),
        ((200, {"ref": f"{BRANCH_REF}-other"}), None, None),
        ((200, []), None, None),
        ((404, {}), None, ""),
        ((403, {}), None, None),
        (httpx2.ConnectError("offline"), None, None),
    ],
)
def test_origin_tip_reads_the_run_branch_over_https(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: tuple[int, object] | Exception,
    token: str | None,
    tip: str | None,
) -> None:
    run = make_run(tmp_path)
    repository = run.request.repo.removeprefix("github.com/")
    url = f"https://api.github.com/repos/{repository}/git/ref/heads/agent/example"
    if isinstance(result, Exception):
        lookup = Mock(side_effect=result)
    else:
        status, body = result
        response = httpx2.Response(
            status, json=body, request=httpx2.Request("GET", url)
        )
        lookup = Mock(return_value=response)
    monkeypatch.setattr(httpx2, "get", lookup)
    monkeypatch.setattr(credentials, "fetch_token", Mock(return_value=token))
    assert host_publication.origin_tip(run) == tip
    headers = {"Accept": "application/vnd.github+json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    lookup.assert_called_once_with(url, headers=headers, timeout=20, verify=ANY)


def test_execute_rejects_a_stale_head_when_requiring_a_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path, expect_commit=True)
    _, clone = repository(tmp_path / "repo")
    git(clone, "switch", "-c", "agent/example")
    prepared = preparation(run)
    prepared = dataclasses.replace(
        prepared,
        paths=dataclasses.replace(prepared.paths, worktree=str(clone)),
        auth_before="example-before",
    )
    HostRuntime(run, prepared, None).patch(monkeypatch)
    monkeypatch.setattr(host_harness, "run_harness", constant(0))
    monkeypatch.setattr(push, "run", PUSH_RUN)
    run.files.write("head", "a" * 40)

    assert host_execution.execute(run, "example") == 6
    assert run.files.path("rc").read_text() == "6\n"
    assert "no new commit: the build required one (--expect-commit)" in (
        run.files.path("log").read_text().splitlines()
    )
    assert not run.files.path("head").exists()


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
    run = Mock(spec=state.Run)
    response = httpx2.Response(
        status, json=body, request=httpx2.Request("GET", "https://example.test")
    )
    lookup = Mock(return_value=response)
    monkeypatch.setattr(host_publication, "origin_tip", Mock(return_value=tip))
    monkeypatch.setattr(host_publication, "github_get", lookup)
    commit = "a" * 40
    assert host_publication.origin_contains(run, commit) is expected
    if tip and tip != commit:
        lookup.assert_called_once_with(run, f"compare/{commit}...{tip}?per_page=1")
    else:
        lookup.assert_not_called()
