# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request publish."""

import json
import re
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import GEN, SUBAGENT
from unit.alpha.pull_request.pr_test_support import (
    OPENER,
    POSTING,
    PR_COMMITS_CALL,
    ROOT,
    TARGET,
    VISIBILITY_READ,
    World,
    base_case,
    edit_cases,
    ledgers,
    pr_page,
    run_main,
    seed_session,
    write_draft,
)

from tbhbdev.alpha import opened
from tbhbdev.alpha.identity import dispatcher as agent_identity
from tbhbdev.alpha.pull_request import attribution as pr_attribution
from tbhbdev.alpha.pull_request.draft import DRAFT
from tbhbdev.alpha.secret_scan import REPO_RULES, without_name_rules

agent = pr_test_support.agent
draft_world = pr_test_support.draft_world
host_names = pr_test_support.host_names
world = pr_test_support.world


def value(**attrs: str) -> str:
    return agent_identity.format_value(attrs)


def test_create_scans_the_final_title_and_body(
    world: World, agent: dict[str, str]
) -> None:
    write_draft(world)
    assert run_main("create", env=agent) == 0
    (call,) = world.calls("gitleaks")
    assert cast("list[str]", call["argv"])[:3] == ["stdin", "--redact", "--no-banner"]
    assert call["cwd"] == str(world.root)
    _, gh = world.calls("gh")
    assert call["stdin"] == f"fix: x\n\n{gh['stdin']}"
    assert str(call["stdin"]).endswith(f"Assisted-by: {POSTING}\n")


def test_edit_scans_the_final_title_and_body(world: World) -> None:
    write_draft(world)
    assert run_main("edit", str(DRAFT), "--pr", "7") == 0
    (call,) = world.calls("gitleaks")
    _, _, gh = world.calls("gh")
    assert call["stdin"] == f"fix: x\n\n{gh['stdin']}"


FINDING = '[{"RuleID": "some-rule", "StartLine": 9}]'


@pytest.mark.parametrize(
    "argv",
    [
        ("create",),
        ("create", "--dry-run"),
        ("edit", str(DRAFT), "--pr", "7"),
    ],
)
def test_a_finding_stops_before_gh(
    world: World, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    world.fake("gitleaks", rc=1, stdout=FINDING)
    write_draft(world)
    with pytest.raises(SystemExit) as info:
        run_main(*argv)
    assert str(info.value) == (
        "refusing the PR title and body: gitleaks finds a secret: "
        "rule some-rule on line 9"
    )
    assert [call["argv"][:2] for call in world.calls("gh")] in (  # type: ignore[index]
        [],
        [["pr", "view"], ["api", "--hostname"]],
    )
    assert "gh pr" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [("create",), ("edit", str(DRAFT), "--pr", "7")])
def test_a_missing_gitleaks_stops_before_gh(
    world: World, argv: tuple[str, ...]
) -> None:
    (world.bin / "gitleaks").unlink()
    write_draft(world)
    with pytest.raises(
        SystemExit, match=r"missing tools: gitleaks|gitleaks not found in \.bazel_env"
    ):
        run_main(*argv)
    assert [call["argv"][:2] for call in world.calls("gh")] in (  # type: ignore[index]
        [],
        [["pr", "view"], ["api", "--hostname"]],
    )


def test_a_missing_private_config_stops_before_gh(world: World) -> None:
    (Path.home() / ".config" / "gitleaks" / "private.toml").unlink()
    write_draft(world)
    with pytest.raises(SystemExit, match=r"private\.toml cannot be read"):
        run_main("create")
    assert world.calls("gitleaks") == []
    assert world.calls("gh") == []


def test_check_does_not_scan_or_read_the_visibility(draft_world: World) -> None:
    (draft_world.bin / "gh").unlink()
    (draft_world.bin / "gitleaks").unlink()
    write_draft(draft_world)
    assert run_main("check") == 0
    assert draft_world.calls("gitleaks") == []
    assert draft_world.calls("gh.repo") == []


def without_path(config: str) -> str:
    """A config with its ``path`` lines dropped: the scan points them at a copy."""
    return re.sub(r"^\s*path\s*=.*$", "", config, flags=re.MULTILINE)


def scan_config(world: World) -> str:
    """The text of the config the one gitleaks call ran under, minus its paths."""
    (call,) = world.calls("gitleaks")
    return without_path(str(call["config"]))


def full_config() -> str:
    """The private config under the test's HOME, minus its paths.

    Every scan drops the repository rules, so this is the config a scan that
    keeps the host rules runs under.
    """
    path = Path.home() / ".config" / "gitleaks" / "private.toml"
    return without_path(str(without_name_rules(path.read_text(), REPO_RULES)))


VISIBILITIES = [
    ("PRIVATE\n", 0, True),
    ("PUBLIC\n", 0, False),
    ("INTERNAL\n", 0, False),
    ("", 0, False),
    ("weird\n", 0, False),
    ("PRIVATE\n", 1, False),
]


@pytest.mark.parametrize("argv", [("create",), ("create", "--dry-run")])
@pytest.mark.parametrize(("stdout", "rc", "reduced"), VISIBILITIES)
def test_create_reads_the_base_repository_once_and_picks_the_config(
    world: World, argv: tuple[str, ...], stdout: str, rc: int, reduced: bool
) -> None:
    cases = {"repo view": base_case(TARGET, stdout.strip(), rc)}
    world.fake("gh", stdout="u\n", cases=cases)
    write_draft(world)
    assert run_main(*argv) == 0
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", "--json", "nameWithOwner,visibility"]
    assert (scan_config(world) != full_config()) is reduced


@pytest.mark.parametrize("name", ["", None, "o/..", "a/b/c", 5, "not a name"])
def test_create_without_a_parsed_base_repository_uses_the_full_config(
    world: World, name: object
) -> None:
    world.fake("gh", stdout="u\n", cases={"repo view": base_case(name, "PRIVATE")})
    write_draft(world)
    assert run_main("create") == 0
    assert len(world.calls("gh.repo")) == 1
    assert scan_config(world) == full_config()


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_create_with_an_unreadable_base_repository_uses_the_full_config(
    world: World, stdout: str
) -> None:
    answer: dict[str, object] = {"rc": 0, "stdout": stdout, "stderr": ""}
    world.fake("gh", stdout="u\n", cases={"repo view": answer})
    write_draft(world)
    assert run_main("create") == 0
    assert len(world.calls("gh.repo")) == 1
    assert scan_config(world) == full_config()


@pytest.mark.parametrize(("stdout", "rc", "reduced"), VISIBILITIES)
def test_edit_reads_the_prs_repository_once_and_picks_the_config(
    world: World, stdout: str, rc: int, reduced: bool
) -> None:
    world.fake("gh", stdout="u\n", cases=edit_cases(pr_page(TARGET), stdout, rc))
    write_draft(world)
    assert run_main("edit", str(DRAFT), "--pr", "7") == 0
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", TARGET, *VISIBILITY_READ]
    assert (scan_config(world) != full_config()) is reduced


def test_a_private_working_directory_posting_to_a_public_repo_gets_the_full_config(
    world: World,
) -> None:
    other = "tbhb-dev/elsewhere"
    cases = edit_cases(pr_page(other))
    cases[f"repo view {other}"] = {"rc": 0, "stdout": "PUBLIC\n", "stderr": ""}
    world.fake("gh", stdout="u\n", cases=cases)
    write_draft(world)
    assert run_main("edit", str(DRAFT), "--pr", "7") == 0
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", other, *VISIBILITY_READ]
    assert scan_config(world) == full_config()


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        7,
        "not a url",
        "https://github.com/o/n/pull/x",
        "http://github.com/o/n/pull/1",
        "https://gitlab.com/o/n/pull/1",
        "https://github.com/o/n/issues/1",
    ],
)
def test_edit_with_an_unresolved_target_refuses_to_post(
    world: World, url: object
) -> None:
    world.fake("gh", stdout="u\n", cases=edit_cases(url, "PRIVATE\n"))
    write_draft(world)
    with pytest.raises(SystemExit, match="cannot determine its GitHub repository"):
        run_main("edit", str(DRAFT), "--pr", "7")
    (view,) = world.calls("gh")
    assert view["argv"] == ["pr", "view", "7", "--json", "body,url"]
    assert world.calls("gh.repo") == []
    assert world.calls("gitleaks") == []


def test_create_writes_opened_by_first_and_the_ledger(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    write_draft(world)
    assert run_main("create", env=agent) == 0
    _, call = world.calls("gh")
    assert str(call["stdin"]).endswith(
        f"\nv\n\n{OPENER}\nGenerated-by: {GEN}\nAssisted-by: {POSTING}\n"
    )
    assert "subagent=" not in OPENER
    path = ledgers(world) / f"{agent_identity.hx(ROOT)}.json"
    assert json.loads(path.read_text()) == {"prs": ["tbhb-dev/tbhbdev#1"]}
    assert capsys.readouterr() == ("https://github.com/tbhb-dev/tbhbdev/pull/1\n", "")
    world.fake("gh", stdout="Created\nhttps://github.com/tbhb-dev/tbhbdev/pull/12\n")
    assert run_main("create", env=agent) == 0
    assert json.loads(path.read_text()) == {
        "prs": ["tbhb-dev/tbhbdev#1", "tbhb-dev/tbhbdev#12"]
    }


@pytest.mark.parametrize("site", ["opener", "create-description", "edit-description"])
def test_pr_commands_report_unresolved_attribution(
    world: World,
    agent: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    site: str,
) -> None:
    write_draft(world)
    agent_identity.record_command(ROOT, SUBAGENT, "background command", world.root)
    if site == "create-description":
        monkeypatch.setattr(
            pr_attribution, "opened_by", Mock(return_value=(None, None))
        )
    argv = (
        ("edit", str(DRAFT), "--pr", "1") if site == "edit-description" else ("create",)
    )
    assert run_main(*argv, env=agent) == 1
    assert capsys.readouterr() == (
        "",
        (
            "tbhbdev alpha pr: Attribution couldn't be resolved. Retry the pull "
            "request command from the main session after the subagent's command "
            "finishes and its session record is available.\n"
        ),
    )
    reads = [call["argv"] for call in world.calls("gh")]
    if site == "edit-description":
        assert reads == [
            ["pr", "view", "1", "--json", "body,url"],
            [*PR_COMMITS_CALL, f"repos/{TARGET}/pulls/1/commits"],
        ]
    else:
        assert reads == []


def test_a_subagent_opens_under_its_root(world: World, agent: dict[str, str]) -> None:
    write_draft(world)
    child = agent_identity.sha256_64("agent-raw")
    agent_identity.write_user_record(child, {"root": ROOT, "model": "claude-x"})
    assert run_main("create", env={**agent, "ASCM_AGENT_ID": child}) == 0
    _, call = world.calls("gh")
    assert str(call["stdin"]).endswith(
        f"\n\n{OPENER}\nAssisted-by: LLM harness=claude-code:2.1.0;"
        f"model=anthropic:claude-x;session={ROOT};subagent={child};surface=cli\n"
    )
    assert opened.read_ledger(ROOT) == {"tbhb-dev/tbhbdev#1"}


@pytest.mark.parametrize(
    "env", [{}, {"ANTIGRAVITY_CONVERSATION_ID": "conv-raw"}], ids=["human", "agy"]
)
def test_no_root_means_no_line_and_no_ledger(
    world: World, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    write_draft(world)
    conv = agent_identity.sha256_64("conv-raw")
    agent_identity.write_user_record(conv, {"model": "gemini-x"})
    assert run_main("create", env=env) == 0
    _, call = world.calls("gh")
    assert "Opened-by" not in str(call["stdin"])
    assert not ledgers(world).exists()
    expected = (
        "warning: no Opened-by line or ledger entry: cannot resolve the root session\n"
        if env
        else ""
    )
    assert capsys.readouterr().err == expected


def test_a_failed_create_records_nothing(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", rc=1, stdout="https://github.com/tbhb-dev/example/pull/1\n")
    write_draft(world)
    assert run_main("create", env=agent) == 1
    assert not ledgers(world).exists()
    assert capsys.readouterr().err == ("https://github.com/tbhb-dev/example/pull/1\n")


def test_create_without_a_url_records_nothing(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", stdout="https://github.com/tbhb-dev/tbhbdev/issues/1\n")
    write_draft(world)
    assert run_main("create", env=agent) == 1
    assert capsys.readouterr().err == (
        "gh printed no pull request URL; nothing was recorded\n"
    )
    assert not ledgers(world).exists()


def test_create_with_a_corrupt_ledger_reports_it(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = ledgers(world) / f"{agent_identity.hx(ROOT)}.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    write_draft(world)
    assert run_main("create", env=agent) == 0
    assert capsys.readouterr().err == (
        "warning: the PR is open, but the ledger was not updated: "
        f"{path} holds no list of pull requests\n"
    )
    assert path.read_text() == "[]"


@pytest.mark.parametrize("make", ["file", "bytes"])
def test_create_warns_when_the_ledger_cant_be_written(
    world: World,
    agent: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    make: str,
) -> None:
    store = ledgers(world)
    store.mkdir(parents=True)
    path = store / f"{agent_identity.hx(ROOT)}.json"
    if make == "file":
        # A directory where the ledger file goes: the write fails with OSError.
        path.mkdir()
        why = f"cannot read {path}: Is a directory"
    else:
        path.write_bytes(b'{"prs": ["\xff"]}')
        why = f"{path} is not UTF-8"
    write_draft(world)
    assert run_main("create", env=agent) == 0
    out, err = capsys.readouterr()
    assert out == "https://github.com/tbhb-dev/tbhbdev/pull/1\n"
    assert err == f"warning: the PR is open, but the ledger was not updated: {why}\n"


def test_create_warns_when_the_write_fails(
    world: World,
    agent: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(opened, "write_private_json", full)
    write_draft(world)
    assert run_main("create", env=agent) == 0
    assert capsys.readouterr().err == (
        "warning: the PR is open, but the ledger was not updated: "
        "[Errno 28] No space left on device\n"
    )


def test_an_opener_without_a_model_gets_no_line_and_no_ledger(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    child = agent_identity.sha256_64("agent-raw")
    agent_identity.write_user_record(child, {"root": ROOT, "model": "claude-x"})
    seed_session(world, None)
    write_draft(world)
    assert run_main("create", env={**agent, "ASCM_AGENT_ID": child}) == 0
    _, call = world.calls("gh")
    assert "Opened-by" not in str(call["stdin"])
    assert not ledgers(world).exists()
    value = f"LLM harness=claude-code:2.1.0;session={ROOT};surface=cli"
    assert capsys.readouterr().err == (
        f"warning: no Opened-by line or ledger entry: {value}: model required\n"
    )
