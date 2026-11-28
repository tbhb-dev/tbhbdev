# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests of the merge queue remove verb with a fake gh."""

import json
import subprocess

import pytest
from unit.merge.fake_github import TARGET, Tools, amend, queue_data, run_main

from tbhbdev.alpha.identity.storage import user_store
from tbhbdev.merge import command, remove, write

NODE = "PR_node12"
REASON = "fails its group; the cause fix is in review"


def verb(*argv: str) -> int | str | None:
    try:
        return remove.main(list(argv))
    except SystemExit as exc:
        return exc.code


def ledger() -> list[dict[str, object]]:
    path = user_store() / "merge-writes.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def dequeues(tools: Tools) -> list[dict[str, object]]:
    return [c for c in tools.calls() if "dequeuePullRequest" in " ".join(c["argv"])]  # type: ignore[arg-type]


@pytest.fixture
def listed(queued_tools: Tools) -> Tools:
    queued_tools.view(
        id=NODE, title="fix: tidy", body="A plain body.\n", baseRefName="release/One"
    )
    amend(
        queued_tools,
        rules_out='[[{"type":"merge_queue","parameters":{"merge_method":"MERGE"}}]]',
        queue_out=queue_data(state="OPEN", isInMergeQueue=True),
    )
    return queued_tools


def test_removes_a_queued_pull_request_and_records_the_reason(
    listed: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verb("12", "--reason", REASON) == 0
    (call,) = dequeues(listed)
    assert f"id={NODE}" in call["argv"]  # type: ignore[operator]
    assert "PR #12" in capsys.readouterr().out
    (entry,) = ledger()
    assert entry["verb"] == "merge-queue-remove"
    assert entry["reason"] == REASON
    assert entry["repo"] == TARGET
    assert entry["pr"] == 12


@pytest.mark.parametrize("flags", [(), ("--reason", ""), ("--reason", "  ")])
def test_refuses_a_missing_or_empty_reason(
    listed: Tools, flags: tuple[str, ...]
) -> None:
    assert verb("12", *flags) not in {0, None}
    assert not (listed.bin / "gh.log").exists()
    assert ledger() == []


def test_refuses_a_closed_pull_request(listed: Tools) -> None:
    listed.view(id=NODE, state="CLOSED", baseRefName="release/One")
    assert "is CLOSED" in str(verb("12", "--reason", REASON))
    assert dequeues(listed) == []


def test_refuses_a_pull_request_not_in_the_queue(listed: Tools) -> None:
    amend(listed, queue_out=queue_data(state="OPEN", isInMergeQueue=False))
    assert "is not in the merge queue" in str(verb("12", "--reason", REASON))
    assert dequeues(listed) == []
    assert ledger() == []


def test_refuses_a_repository_without_a_merge_queue(listed: Tools) -> None:
    amend(listed, rules_out='[[{"type": "pull_request"}]]')
    assert "has no merge queue" in str(verb("12", "--reason", REASON))
    assert dequeues(listed) == []


def test_dry_run_shows_the_call_and_changes_nothing(
    listed: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verb("12", "--reason", REASON, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dequeuePullRequest" in out
    assert dequeues(listed) == []
    assert ledger() == []


@pytest.mark.usefixtures("listed")
def test_both_verbs_write_through_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None]] = []

    def spy(
        name: str, _pull: tuple[str, int], argv: list[str], **kw: str | None
    ) -> subprocess.CompletedProcess[str]:
        seen.append((name, kw.get("reason")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(write, "gh_write", spy)
    verb("12", "--reason", REASON)
    run_main("12")
    assert [name for name, _ in seen] == ["merge-queue-remove", "merge"]
    assert seen[0][1] == REASON
    assert command.write is write


@pytest.mark.usefixtures("tools")
def test_merge_writes_a_ledger_entry_without_a_reason() -> None:
    assert run_main("12") == 0
    (entry,) = ledger()
    assert (entry["verb"], entry["pr"], entry["reason"]) == ("merge", 12, None)


def test_a_failed_dequeue_is_reported_and_not_recorded(listed: Tools) -> None:
    amend(listed, dequeue_rc=1, dequeue_err="boom\n")
    assert "gh dequeue failed: boom" in str(verb("12", "--reason", REASON))
    assert ledger() == []


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"view_rc": 1, "view_err": "boom"}, "gh pr view failed: boom"),
        ({"view_out": "not json"}, "no usable pull request"),
        ({"view_out": "{}"}, "no usable pull request"),
        (
            {"view_out": json.dumps({
                "id": NODE, "state": "OPEN", "baseRefName": "main", "url": "x"
            })},
            "which repository",
        ),
    ],
)  # fmt: skip
def test_refuses_what_gh_cannot_describe(
    listed: Tools, spec: dict[str, object], message: str
) -> None:
    amend(listed, **spec)
    assert message in str(verb("12", "--reason", REASON))
    assert dequeues(listed) == []


def test_a_ledger_that_cannot_be_written_only_warns(
    listed: Tools,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(_entry: dict[str, object]) -> None:
        message = "read-only"
        raise OSError(message)

    monkeypatch.setattr(write, "record", refuse)
    assert verb("12", "--reason", REASON) == 0
    assert "the write ledger was not updated: read-only" in capsys.readouterr().err
    assert len(dequeues(listed)) == 1
