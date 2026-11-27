# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests of the merge command with fake gh, cog and gitleaks."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from fake_tool import pythonpath
from unit.alpha.merge.examples import (
    ASSIST,
    GEN,
    OPENED,
    RENOVATE_AUTHOR,
    RENOVATE_BODY,
)
from unit.alpha.merge.fake_github import (
    APPROVED_HEAD,
    OTHER_HEAD,
    TARGET,
    Tools,
    amend,
    commit,
    queue_data,
    queue_rules,
    read_back,
    repo_reads,
    run_main,
)

from tbhbdev.alpha.identity.dispatcher import AGENT_EMAIL
from tbhbdev.alpha.merge import command
from tbhbdev.alpha.merge.command import main
from tbhbdev.alpha.merge.github import QUEUE_QUERY, VIEW_FIELDS
from tbhbdev.alpha.secret_scan import REPO_RULES, without_name_rules

SRC = Path(command.__file__).parents[3]

HARNESS_ENV = {"CLAUDE_CODE_SESSION_ID": "raw-session"}


AGENT_COMMITS = [
    commit(OTHER_HEAD, "human@example.com"),
    commit(APPROVED_HEAD, "other@example.com", AGENT_EMAIL),
]


def without_path(config: str) -> str:
    """A config with its ``path`` lines dropped: the scan points them at a copy."""
    return re.sub(r"^\s*path\s*=.*$", "", config, flags=re.MULTILINE)


def scan_config(tools: Tools) -> str:
    """The text of the config the one gitleaks call ran under, minus its paths."""
    (scanned,) = tools.scanned()
    return without_path(cast("str", scanned["config"]))


def full_config() -> str:
    """The private config under the test's HOME, minus its paths.

    Every scan drops the repository rules, so this is the config a scan that
    keeps the host rules runs under.
    """
    path = Path.home() / ".config" / "gitleaks" / "private.toml"
    return without_path(str(without_name_rules(path.read_text(), REPO_RULES)))


@pytest.mark.parametrize("flags", [(), ("--expect-head", APPROVED_HEAD.upper())])
def test_dry_run_prints_the_command_and_message_and_merges_nothing(
    tools: Tools, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    code = run_main("12", "--dry-run", *flags)
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(
        "gh pr merge 12 --merge --subject 'feat(cli): add the stamp subcommand (#12)' "
        f"--body-file - --match-head-commit {APPROVED_HEAD}\n\n"
        "feat(cli): add the stamp subcommand (#12)\n\nWhy\n"
    )
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


@pytest.mark.parametrize("flags", [(), ("--expect-head", APPROVED_HEAD)])
def test_merge_passes_subject_body_and_head(
    tools: Tools, flags: tuple[str, ...]
) -> None:
    assert run_main("12", *flags) == 0
    view, merge = tools.calls()
    assert view["argv"] == ["pr", "view", "12", "--json", VIEW_FIELDS]
    assert merge["argv"] == [
        "pr", "merge", "12", "--merge",
        "--subject", "feat(cli): add the stamp subcommand (#12)",
        "--body-file", "-", "--match-head-commit", APPROVED_HEAD,
    ]  # fmt: skip
    assert str(merge["stdin"]).startswith("Why\nRunning `tool stamp`")
    assert str(merge["stdin"]).endswith(
        "[1]: https://github.com/tbhb-dev/tbhbdev/issues/12\n"
    )


def test_agent_merge_refuses_a_missing_pre_push_hook(
    tools: Tools, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (Path.cwd() / ".git/hooks/pre-push").unlink()
    monkeypatch.setenv("CODEX_SESSION_ID", "test-session")
    assert run_main("12") == 1
    assert not (tools.bin / "gh.log").exists()
    err = capsys.readouterr().err
    assert "pre-push" in err
    assert "tbhbdev alpha install" in err


def test_merge_scans_the_final_message(tools: Tools, tmp_path: Path) -> None:
    assert run_main("12") == 0
    merge = tools.calls()[1]
    subject = "feat(cli): add the stamp subcommand (#12)"
    (scanned,) = tools.scanned()
    assert scanned["cwd"] == str(tmp_path / "repo")
    assert scanned["stdin"] == f"{subject}\n\n{merge['stdin']}"
    assert scan_config(tools) == full_config()


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_finding_in_the_current_body_stops_the_merge(
    tools: Tools,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    finding = '[{"RuleID": "some-rule", "StartLine": 3}]'
    (tools.bin / "gitleaks.spec").write_text(json.dumps([1, finding]))
    argv = ["12", "--dry-run"] if dry_run else ["12"]
    code = run_main(*argv)
    assert code == (
        "refusing the commit message for PR #12: gitleaks finds a secret: "
        "rule some-rule on line 3"
    )
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]
    assert "gh pr merge" not in capsys.readouterr().out


def test_a_missing_gitleaks_stops_the_merge(tools: Tools) -> None:
    (tools.bin / "gitleaks").unlink()
    code = run_main("12")
    assert str(code).startswith("gitleaks not found in .bazel_env")
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


def test_a_missing_private_config_stops_the_merge(tools: Tools, tmp_path: Path) -> None:
    (tmp_path / ".config" / "gitleaks" / "private.toml").unlink()
    code = run_main("12")
    assert "private.toml cannot be read" in str(code)
    assert tools.scanned() == []
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


def test_merge_reports_gh_output_and_exit_code(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    tools.view()
    spec = json.loads((tools.bin / "gh.spec").read_text())
    tools.gh(**{**spec, "merge_rc": 4, "merge_out": "o\n", "merge_err": "e\n"})
    assert run_main("3") == 4
    assert len(tools.calls()) == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("o\n", "e\n")


def test_null_body_is_refused(tools: Tools) -> None:
    tools.view(body=None)
    assert "GitHub's default message" in str(run_main("3"))


def test_a_renovate_pr_merges_with_the_update_list(tools: Tools) -> None:
    tools.view(
        title="chore(deps): update dependency macos to v26",
        body=RENOVATE_BODY,
        author=RENOVATE_AUTHOR,
    )
    assert run_main("6") == 0
    assert tools.calls()[1]["stdin"] == "- macos 15 → 26 (major)\n"


@pytest.mark.parametrize(
    "author",
    [
        {"is_bot": False, "login": "app/tbhb-dev-renovate"},
        {"is_bot": True, "login": "app/dependabot"},
        {"login": "app/tbhb-dev-renovate"},
        "app/tbhb-dev-renovate",
        None,
    ],
)
def test_only_the_renovate_app_gets_the_short_body(
    tools: Tools, author: object
) -> None:
    tools.view(body="## Why\n\nBecause.\n", author=author)
    assert run_main("6") == 0
    assert tools.calls()[1]["stdin"] == "Why\nBecause.\n"


@pytest.mark.parametrize(
    "env",
    [
        HARNESS_ENV,
        {"CODEX_SESSION_ID": "s"},
        {"ANTIGRAVITY_CONVERSATION_ID": "c"},
    ],
)
def test_an_agent_environment_changes_nothing(
    tools: Tools, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main("12") == 0
    plain = tools.calls()
    (tools.bin / "gh.log").unlink()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert run_main("12") == 0
    assert tools.calls() == plain
    assert [call["argv"][1] for call in plain] == ["view", "merge"]  # type: ignore[index]


def test_a_dry_run_is_the_same_in_an_agent_environment(
    tools: Tools, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main("12", "--dry-run") == 0
    plain = capsys.readouterr().out
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "raw")
    assert run_main("12", "--dry-run") == 0
    assert capsys.readouterr().out == plain
    assert [call["argv"][1] for call in tools.calls()] == ["view", "view"]  # type: ignore[index]


def test_the_script_never_adds_human_trailers(tools: Tools) -> None:
    assert run_main("12") == 0
    stdin = str(tools.calls()[1]["stdin"])
    assert not re.search(r"Signed-off-by|Co-authored-by", stdin, re.IGNORECASE)


@pytest.mark.usefixtures("tools")
def test_runs_as_tbhbdev_alpha_merge(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": pythonpath(SRC)}
    env.pop("MUTANT_UNDER_TEST", None)
    done = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "alpha", "merge", "3", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path / "repo",
        env=env,
    )
    assert done.returncode == 0
    assert "gh pr merge 3" in done.stdout


def test_a_body_with_opened_by_merges_without_it(tools: Tools) -> None:
    tools.view(body=f"## Why\n\nw\n\n{OPENED}\n{GEN}\n")
    assert main(["7"]) == 0
    merge = tools.calls()[-1]
    assert cast("list[str]", merge["argv"])[:2] == ["pr", "merge"]
    assert merge["stdin"] == f"Why\nw\n\n{GEN}\n"


@pytest.mark.parametrize("argv", [("12",), ("12", "--dry-run")])
@pytest.mark.parametrize(
    ("out", "rc", "reduced"),
    [
        ("PRIVATE\n", 0, True),
        ("PUBLIC\n", 0, False),
        ("INTERNAL\n", 0, False),
        ("", 0, False),
        ("weird\n", 0, False),
        ("PRIVATE\n", 1, False),
    ],
)
def test_the_visibility_is_read_once_and_picks_the_config(
    tools: Tools, argv: tuple[str, ...], out: str, rc: int, reduced: bool
) -> None:
    spec = tools.bin / "gh.spec"
    answer = {"repo_out": out, "repo_rc": rc, "repo_err": "boom" if rc else ""}
    spec.write_text(json.dumps(json.loads(spec.read_text()) | answer))
    assert run_main(*argv) == 0
    (read,) = repo_reads(tools)
    assert read["argv"] == [
        "repo", "view", TARGET, "--json", "visibility", "--jq", ".visibility"
    ]  # fmt: skip
    assert (scan_config(tools) != full_config()) is reduced


def test_a_private_working_directory_merging_into_a_public_repo_gets_the_full_config(
    tools: Tools,
) -> None:
    other = "tbhb-dev/elsewhere"
    tools.view(url=f"https://github.com/{other}/pull/12")
    assert run_main("12") == 0
    (read,) = repo_reads(tools)
    assert read["argv"][2] == other  # type: ignore[index]
    assert scan_config(tools) == full_config()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        (AGENT_COMMITS, "## Why\n\nw\n", "missing final trailer paragraph"),
        (
            AGENT_COMMITS,
            f"## Why\n\nw\n\n{ASSIST}\n{GEN}\n",
            "Generated-by must come before Assisted-by",
        ),
        (
            [commit()],
            f"## Why\n\nw\n\n{ASSIST}\n{GEN}\n",
            "Generated-by must come before Assisted-by",
        ),
    ],
)
def test_invalid_pr_trailers_refuse_before_preview_or_merge(
    tools: Tools,
    capsys: pytest.CaptureFixture[str],
    case: tuple[list[dict[str, object]], str, str],
    dry_run: bool,
) -> None:
    commits, body, reason = case
    tools.view(commits=commits, body=body)
    args = ["12", "--dry-run"] if dry_run else ["12"]
    code = run_main(*args)
    assert isinstance(code, str)
    assert reason in code
    if reason == "missing final trailer paragraph":
        assert code == (
            "the PR body cannot be merged as is: "
            "agent PR is missing final trailer paragraph; "
            "add a valid trailer paragraph with tbhbdev alpha pr edit"
        )
    assert "tbhbdev alpha pr edit" in code
    assert capsys.readouterr().out == ""
    (view,) = tools.calls()
    assert cast("list[str]", view["argv"])[:2] == ["pr", "view"]
    assert tools.scanned() == []


@pytest.mark.parametrize("block", [GEN, ASSIST, f"{GEN}\n{ASSIST}"])
def test_an_agent_pr_merges_with_its_body_trailers(tools: Tools, block: str) -> None:
    tools.view(commits=AGENT_COMMITS, body=f"## Why\n\nw\n\n{block}\n")
    assert main(["12"]) == 0
    view, merge = tools.calls()
    assert cast("list[str]", view["argv"])[-1] == VIEW_FIELDS
    assert "commits" in VIEW_FIELDS.split(",")
    assert merge["stdin"] == f"Why\nw\n\n{block}\n"
    assert read_back(f"fix: x\n\n{merge['stdin']}") == f"{block}\n"


@pytest.mark.parametrize(
    "commits",
    [[commit()], [commit(APPROVED_HEAD, "human@example.com")]],
)
def test_a_non_agent_pr_needs_no_trailer_block(
    tools: Tools, commits: list[dict[str, object]]
) -> None:
    tools.view(commits=commits, body="## Why\n\nw\n")
    assert main(["12"]) == 0
    assert tools.calls()[1]["stdin"] == "Why\nw\n"


def test_renovate_keeps_its_path_even_with_an_agent_commit(tools: Tools) -> None:
    tools.view(
        commits=AGENT_COMMITS,
        author=RENOVATE_AUTHOR,
        body=RENOVATE_BODY,
    )
    assert main(["12"]) == 0
    assert tools.calls()[1]["stdin"] == "- macos 15 → 26 (major)\n"


def test_dry_run_preview_is_exact(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    tools.view(title="fix: x", body="## Why\n\nw\n")
    assert main(["12", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "gh pr merge 12 --merge --subject 'fix: x (#12)' "
        f"--body-file - --match-head-commit {APPROVED_HEAD}\n\n"
        "fix: x (#12)\n\nWhy\nw\n"
    )


def test_main_reads_process_arguments_and_keeps_the_program_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["merge-preview", "--help"])
    with pytest.raises(SystemExit) as info:
        main(prog="custom-merge")
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: custom-merge ")
    assert "[--expect-head SHA]" in out
    assert "Merge a PR with a clean merge commit message." in out


def test_main_reports_every_refusal_before_scanning_or_printing(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    tools.view(
        state="CLOSED",
        isDraft=True,
        mergeable="CONFLICTING",
        mergeStateStatus="BLOCKED",
    )
    with pytest.raises(SystemExit) as info:
        main(["37", "--dry-run"])
    assert str(info.value) == (
        "not merging PR #37: the PR is closed; the PR is a draft; "
        "gh reports mergeable=CONFLICTING; gh reports mergeStateStatus=BLOCKED"
    )
    assert capsys.readouterr().out == ""
    assert len(tools.calls()) == 1
    assert repo_reads(tools) == []
    assert tools.scanned() == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_main_passes_the_full_message_and_target_visibility_to_the_scan(
    tools: Tools, dry_run: bool
) -> None:
    tools.view(title="fix: x", body="## Why\n\nw\n")
    amend(tools, repo_out="PRIVATE\n")
    args = ["37", "--dry-run"] if dry_run else ["37"]
    assert main(args) == 0
    (scanned,) = tools.scanned()
    assert scanned["stdin"] == "fix: x (#37)\n\nWhy\nw\n"
    assert scan_config(tools) != full_config()


def test_an_empty_body_refuses_the_merge_method(tools: Tools) -> None:
    tools.view(title="fix: x", body="")
    code = run_main("12")
    assert "GitHub's default message" in str(code)
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


def test_a_default_form_subject_refuses_the_merge_method(tools: Tools) -> None:
    tools.view(title="Merge pull request #5 from x/y", body="## Why\n\nw\n")
    code = run_main("12")
    assert "GitHub's default message" in str(code)
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


@pytest.mark.parametrize("method", ["MERGE", "merge"])
def test_merge_queue_dry_run_prints_the_cleaned_message(
    tools: Tools, capsys: pytest.CaptureFixture[str], method: str
) -> None:
    tools.view(title="fix: tidy", body="A plain body.\n")
    amend(
        tools,
        rules_out=queue_rules(method),
        queue_out=queue_data(),
    )
    assert run_main("12", "--dry-run") == 0
    # A MERGE queue keeps the subject and body passed to gh pr merge.
    assert capsys.readouterr().out == (
        "gh pr merge 12 --merge --subject 'fix: tidy (#12)' "
        f"--body-file - --match-head-commit {APPROVED_HEAD}\n\n"
        "fix: tidy (#12)\n\nA plain body.\n"
    )
    _, before = tools.calls()
    assert cast("list[str]", before["argv"])[-1] == "number=12"


def test_merge_queue_refuses_a_default_message(tools: Tools) -> None:
    tools.view(title="fix: tidy", body="")
    amend(tools, rules_out=queue_rules("MERGE"))
    assert run_main("12") == (
        "not merging PR #12: the merge commit would get "
        "GitHub's default message, not the cleaned PR body"
    )
    assert len(tools.calls()) == 1


@pytest.mark.parametrize(
    ("state", "code", "output"),
    [
        (queue_data(state="OPEN", isInMergeQueue=True), 0, "PR #12 queued\n"),
        (
            queue_data(state="MERGED", mergeCommit={"oid": "def456"}),
            0,
            "PR #12 merged as def456\n",
        ),
        (
            queue_data(state="OPEN", removal={"id": "new", "reason": "CI failed"}),
            "PR #12 dequeued: CI failed",
            "",
        ),
        (
            queue_data(state="OPEN", removal={"id": "old", "reason": "Old"}),
            "PR #12: GitHub reports no merge commit, queue entry, or new removal",
            "",
        ),
    ],
)
def test_merge_queue_reports_the_result_using_the_previous_removal(
    tools: Tools,
    capsys: pytest.CaptureFixture[str],
    state: str,
    code: int | str,
    output: str,
) -> None:
    tools.view(title="fix: tidy", body="A plain body.\n")
    amend(
        tools,
        rules_out=queue_rules("MERGE"),
        queue_before_out=queue_data(removal={"id": "old", "reason": "Old"}),
        queue_out=state,
        merge_err="GitHub accepted the request.\n",
    )
    assert run_main("12") == code
    captured = capsys.readouterr()
    assert captured.out == output
    assert captured.err == "GitHub accepted the request.\n"
    _, before, merge, after = tools.calls()
    assert cast("list[str]", before["argv"])[-1] == "number=12"
    assert cast("list[str]", merge["argv"])[:4] == ["pr", "merge", "12", "--merge"]
    assert before == after


def test_merge_refuses_failed_queue_lookup(queued_tools: Tools) -> None:
    amend(queued_tools, queue_rc=1, queue_err="queue\n  unavailable\n")
    assert run_main("12") == "gh merge queue lookup failed: queue unavailable"
    _, lookup = queued_tools.calls()
    assert cast("list[str]", lookup["argv"])[:2] == ["api", "graphql"]
    assert queued_tools.scanned() == []


def test_a_queue_merge_refusal_preserves_the_gh_error(
    queued_tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    amend(queued_tools, merge_rc=1, merge_out="", merge_err="head changed\n")
    assert run_main("12") == 1
    assert capsys.readouterr().err == "head changed\n"
    assert len(queued_tools.calls()) == 3


def test_merge_queue_enqueues_with_the_matched_head(
    queued_tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main("12") == 0
    assert capsys.readouterr().out == "PR #12 queued\n"
    rules = json.loads((queued_tools.bin / "gh.rules.log").read_text())
    assert rules["argv"] == [
        "api", "--paginate", "--slurp", f"repos/{TARGET}/rules/branches/release%2FOne",
    ]  # fmt: skip
    view, before, merge, after = queued_tools.calls()
    owner, repo = TARGET.split("/", 1)
    assert view["argv"] == ["pr", "view", "12", "--json", VIEW_FIELDS]
    assert before["argv"] == [
        "api", "graphql", "-f", f"query={QUEUE_QUERY}", "-f", f"owner={owner}",
        "-f", f"repo={repo}", "-F", "number=12",
    ]  # fmt: skip
    assert merge == {
        "argv": [
            "pr",
            "merge",
            "12",
            "--merge",
            "--subject",
            "fix: tidy (#12)",
            "--body-file",
            "-",
            "--match-head-commit",
            APPROVED_HEAD,
        ],
        "stdin": "A plain body.\n",
    }
    assert before == after
