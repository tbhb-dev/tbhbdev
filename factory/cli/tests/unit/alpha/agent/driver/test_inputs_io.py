"""Exercise coordinator inputs through fake tools and run records."""

from __future__ import annotations

import pytest
from agent_cli_example import run_agent
from agent_driver_example import (
    BODY,
    Remote,
    write_standalone_fake,
)
from driver_scenarios import (
    COMMIT,
    arguments,
    launch,
)

from tbhbdev import telemetry
from tbhbdev.alpha.agent.driver import (
    metadata,
)


def test_schema_and_prompt_refused_locally(remote: Remote) -> None:
    bad = remote.root / "bad.json"
    bad.write_text("{")
    assert launch(remote, "--schema", str(bad)) == 1
    assert remote.records("ssh") == []
    remote.prompt.rename(remote.prompt.with_suffix(".saved"))
    assert launch(remote) == 1
    assert remote.records("ssh") == []


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        ("op://example/item/field", "1Password reference"),
        ("-----BEGIN " + "PRIVATE KEY-----", "private key header"),
        ("-----BEGIN OPENSSH " + "PRIVATE KEY-----", "private key header"),
        ("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----", "private key header"),
        ("ghp_" + "sample", "token prefix"),
        ("github_pat_" + "sample", "token prefix"),
        ("sk-ant-" + "sample", "token prefix"),
        ("AKIA" + "SAMPLE", "token prefix"),
        ("xoxb-" + "sample", "token prefix"),
    ],
)
def test_prompt_with_secret_is_refused_before_ssh(
    remote: Remote, capsys: pytest.CaptureFixture[str], secret: str, kind: str
) -> None:
    remote.prompt.write_text(f"Use this value: {secret}\n")
    assert launch(remote) == 1
    assert capsys.readouterr() == ("", f"tbhbdev alpha agent: brief refused: {kind}\n")
    assert remote.records("ssh", limits=True) == []
    assert not metadata.metadata_path("sample").exists()


def test_schema_with_secret_is_refused_before_ssh(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = remote.root / "schema.json"
    schema.write_text('{"description":"op://example/item/field"}')
    assert launch(remote, "--schema", str(schema)) == 1
    assert capsys.readouterr() == (
        "",
        "tbhbdev alpha agent: schema refused: 1Password reference\n",
    )
    assert remote.records("ssh", limits=True) == []


def test_prompt_hook_conflicts_refuse_launch_before_ssh(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attributes: dict[str, str] = {}
    monkeypatch.setattr(telemetry, "attributes", attributes.update)
    remote.prompt.write_text("Run `prek install`. Run `git push origin main`.\n")
    args = arguments(remote, "--ignore-limits")
    args[-2] = "sample-run"
    assert run_agent(args) == 1
    assert attributes == {
        "tbhb.agent.host": "example-linux",
        "tbhb.agent.harness": "codex",
        "tbhb.agent.model": "example-model",
        "tbhb.agent.profile": "build",
        "tbhb.run.id": "sample-run",
        "tbhb.agent.outcome": "refused",
    }
    assert capsys.readouterr() == (
        "",
        (
            f"tbhbdev alpha agent: {remote.prompt}: "
            "hook conflict: push to main or master is forbidden\n"
            f"{remote.prompt}: hook conflict: hook install is forbidden\n"
            f"{remote.prompt}: the brief directs work to main. "
            "Use the launcher branch agent/sample-run.\n"
        ),
    )
    assert remote.records("ssh", limits=True) == []
    assert remote.records("tmux") == []
    assert not metadata.metadata_path("sample-run").exists()


def test_prompt_without_hook_conflicts_launches(remote: Remote) -> None:
    remote.prompt.write_text(
        'Never run `prek install`. Use `git commit -m "feat: x"`.\n'
    )
    assert launch(remote) == 0
    assert metadata.metadata_path("sample").is_file()
    assert (remote.state / "sample.prompt").read_text() == remote.prompt.read_text()


@pytest.mark.parametrize(
    ("prompt", "options"),
    [
        ("Create branch `agent/other`.", ("--profile", "review", "--ref", COMMIT)),
        ("The earlier PR was on `agent/other`.", ()),
    ],
)
def test_prompt_with_foreign_branch_warns_and_launches(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    prompt: str,
    options: tuple[str, ...],
) -> None:
    write_standalone_fake(remote.bin / "tmux", BODY.replace("sample", "sample-run"))
    remote.prompt.write_text(prompt)
    args = arguments(remote, *options)
    args[-2] = "sample-run"
    assert run_agent(args) == 0
    assert capsys.readouterr().err == (
        f"warning: {remote.prompt}: use the launcher branch agent/sample-run "
        "instead of agent/other\n"
    )
    assert metadata.metadata_path("sample-run").is_file()


@pytest.mark.parametrize(
    ("prompt", "options", "unfetched"),
    [
        ("Use `refs/pull/5/head`.", ("--fetch-ref", "refs/pull/5/head"), ()),
        ("Use `agent/other`.", ("--fetch-ref", "refs/heads/agent/other"), ()),
        (
            ("See refs/heads/foo/bar-1.x, and refs/heads/Release_2..."),
            (),
            ("refs/heads/foo/bar-1.x", "refs/heads/Release_2"),
        ),
        ("Read the README.", (), ()),
        ("See refs/tags/x or refs/pull/abc/head.", (), ()),
        (
            (
                "See refs/pull/5/heads, refs/pull/5/head.old, refs/pull/5/head/old, "
                "refs/pull/5/head~2, or origin/refs/pull/5/head."
            ),
            (),
            (),
        ),
    ],
)
def test_prompt_warns_about_unfetched_refs_and_launches(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    prompt: str,
    options: tuple[str, ...],
    unfetched: tuple[str, ...],
) -> None:
    remote.prompt.write_text(prompt)
    assert launch(remote, *options) == 0
    assert capsys.readouterr() == (
        "sample: launched host=example-linux account=agent\n",
        "".join(
            f"warning: {remote.prompt}: the prompt names {ref} but this launch has no "
            f"--fetch-ref {ref}; a run whose mirror lacks it may end with no work\n"
            for ref in unfetched
        ),
    )


@pytest.mark.parametrize(
    ("ref", "pinned"),
    [
        ("refs/heads/x/", False),
        ("refs/heads/x//y", False),
        ("refs/pull/5/head", True),
        ("refs/heads/agent/topic", True),
        ("refs/tags/topic", True),
    ],
)
def test_ref_warning_malformed_and_pinned(
    remote: Remote, capsys: pytest.CaptureFixture[str], ref: str, *, pinned: bool
) -> None:
    remote.prompt.write_text(f"Run `git checkout {ref}`. See `{ref}`.\n")
    if pinned:
        remote.configure(refs=f"{COMMIT}\t{ref}\n")
        assert launch(remote, "--profile", "review", "--ref", ref) == 0
        assert capsys.readouterr().err == ""
    else:
        assert launch(remote) == 0
        assert capsys.readouterr().err == (
            f"warning: {remote.prompt}: malformed ref {ref!r} in the prompt\n"
        )


@pytest.mark.parametrize(
    ("prompt", "options", "ref"),
    [
        ("See (refs/pull/5/head); reuse refs/pull/5/head.", (), "refs/pull/5/head"),
        ("Use refs/heads/agent/topic.", (), "refs/heads/agent/topic"),
        (
            "Use `agent/Topic`.",
            ("--fetch-ref", "refs/heads/agent/topic"),
            "refs/heads/agent/Topic",
        ),
        (
            "Use agent/topic.x.",
            ("--fetch-ref", "refs/heads/agent/topic"),
            "refs/heads/agent/topic.x",
        ),
    ],
)
def test_prompt_refuses_unfetched_exact_refs_before_ssh(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    prompt: str,
    options: tuple[str, ...],
    ref: str,
) -> None:
    remote.prompt.write_text(prompt)
    assert launch(remote, *options) == 1
    assert capsys.readouterr().err == (
        f"tbhbdev alpha agent: {remote.prompt}: the brief names {ref}. "
        f"Add `--fetch-ref {ref}`\n"
    )
    assert remote.records("ssh") == []
    assert not metadata.metadata_path("sample").exists()
