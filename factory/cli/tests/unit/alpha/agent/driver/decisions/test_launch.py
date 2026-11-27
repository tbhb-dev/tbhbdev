"""Test coordinator launch decisions with literal values."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import LINUX, settings
from driver_run_example import BUILD

from tbhbdev.alpha.agent.driver.decisions import launch
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile
from tbhbdev.alpha.identity.models import AGY_MODELS

CONFIG = settings()
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("existing_name", "ref", "message"),
    [
        (
            "example",
            None,
            (
                "run metadata already exists. "
                "Run `tbhbdev alpha agent clean --force example`"
            ),
        ),
        (
            None,
            "main",
            (
                "--ref applies only to a review run. "
                "For a build run use --fetch-ref REF --start-ref REF"
            ),
        ),
    ],
)
def test_check_refuses(
    existing_name: str | None, ref: str | None, message: str
) -> None:
    with pytest.raises(Invalid) as caught:
        launch.check(BUILD, CONFIG, LINUX, existing_name=existing_name, ref=ref)
    assert str(caught.value) == message


@pytest.mark.parametrize("profile", [None, "Primary", "primary"])
def test_profile(profile: str | None) -> None:
    run = dataclasses.replace(
        BUILD, harness=Harness.CLAUDE, reason="example", claude_profile=profile
    )
    assert launch.check(
        run, CONFIG, LINUX, existing_name=None, ref=None
    ) == dataclasses.replace(run, claude_profile="primary")


def test_unknown_profile() -> None:
    run = dataclasses.replace(BUILD, harness=Harness.CLAUDE, claude_profile="Missing")
    with pytest.raises(Invalid, match="claude profile 'Missing' isn't defined"):
        launch.check(run, CONFIG, LINUX, existing_name=None, ref=None)


def test_review_ref_allowed() -> None:
    run = dataclasses.replace(BUILD, profile=Profile.REVIEW, commit=COMMIT)
    assert launch.check(run, CONFIG, LINUX, existing_name=None, ref="topic") == run
    assert launch.check(BUILD, CONFIG, LINUX, existing_name=None, ref=None) == BUILD


@pytest.mark.parametrize(
    ("local", "remote", "matches"),
    [("one", "one", True), ("one", "two", False), ("", "one", False)],
)
def test_version(local: str, remote: str, matches: bool) -> None:
    if matches:
        assert launch.same_version(local, remote) is None
    else:
        with pytest.raises(Invalid) as caught:
            launch.same_version(local, remote)
        assert (
            str(caught.value) == f"version mismatch: driver {local!r}; host {remote!r}"
        )


@pytest.mark.parametrize(
    ("ref", "text", "result"),
    [
        (COMMIT, "", COMMIT),
        ("a" * 64, "", "a" * 64),
        ("topic", f"{COMMIT}\trefs/heads/topic\n", COMMIT),
        ("tag", f"{'a' * 40}\trefs/tags/tag\n{COMMIT}\trefs/tags/tag^{{}}\n", COMMIT),
        ("topic", "", None),
        ("topic", "bad refs/heads/topic\n", None),
        ("topic", f"{COMMIT} refs/heads/topic\n{'b' * 40} refs/tags/topic\n", None),
    ],
)
def test_ref(ref: str, text: str, result: str | None) -> None:
    if result is None:
        with pytest.raises(Invalid) as caught:
            launch.resolve_ref(ref, text)
        assert str(caught.value) == f"ref {ref!r} doesn't resolve to one commit"
    else:
        assert launch.resolve_ref(ref, text) == result


@pytest.mark.parametrize(
    ("harness", "model", "allow", "valid"),
    [
        (Harness.CODEX, "example", False, True),
        (Harness.CLAUDE, "example", False, True),
        (Harness.AGY, "gemini-example", False, True),
        (Harness.AGY, "example", True, True),
        (Harness.AGY, "example", False, False),
        (Harness.AGY, "gemini", False, False),
    ],
)
def test_model(harness: Harness, model: str, allow: bool, valid: bool) -> None:
    if valid:
        assert launch.check_model(harness, model, allow_non_gemini=allow) is None
    else:
        with pytest.raises(Invalid) as caught:
            launch.check_model(harness, model, allow_non_gemini=allow)
        rule, heading, *models = str(caught.value).splitlines()
        assert rule == "agy requires a gemini-* model or --allow-non-gemini"
        assert heading == "Models reported by agy"
        assert [model.strip() for model in models] == list(AGY_MODELS)


def test_unsandboxed_host_flag_does_not_gate_validation() -> None:
    run = dataclasses.replace(BUILD, unsandboxed=True, authorized_by=None)
    assert launch.check(run, CONFIG, LINUX, existing_name=None, ref=None) == run
    assert (
        launch.check(
            run,
            CONFIG,
            dataclasses.replace(LINUX, unsandboxed=False),
            existing_name=None,
            ref=None,
        )
        == run
    )


def test_peeled_tag_does_not_hide_an_ambiguous_branch() -> None:
    text = (
        f"{'b' * 40} refs/heads/topic\n{'a' * 40} refs/tags/topic\n"
        f"{COMMIT} refs/tags/topic^{{}}\n"
    )
    with pytest.raises(Invalid) as caught:
        launch.resolve_ref("topic", text)
    assert str(caught.value) == "ref 'topic' doesn't resolve to one commit"


def test_ref_branch_and_tag_may_share_a_commit() -> None:
    refs = f"{COMMIT} refs/heads/topic\n{COMMIT} refs/tags/topic\n"
    assert launch.resolve_ref("topic", refs) == COMMIT
