"""Check updates to a build's starting branch and preflight records."""

import io
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev.alpha import pre_push_guard as pg
from tbhbdev.alpha import preflight_gate

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "case",
    [
        ("fix/topic", "ba", 0, ""),
        ("agent/example", "ba", 0, "this run may only push refs/heads/fix/topic"),
        (
            "fix/topic",
            "0a",
            0,
            "deletion refused; this run may only update its start ref",
        ),
        ("fix/topic", "b0", 0, "the start ref must already exist; push refused"),
        ("fix/topic", "ba", 1, ""),
        (
            "fix/topic",
            "ba",
            128,
            "git merge-base could not check ancestry; push refused",
        ),
        # The advertised tip c is an ancestor of the fetched start a and the fix b.
        ("fix/topic", "bc", 0, "remote tip changed since fetch; push refused"),
    ],
)
def test_start_ref_push(
    monkeypatch: pytest.MonkeyPatch, case: tuple[str, str, int, str]
) -> None:
    branch, shas, ancestry, expected = case
    monkeypatch.setenv(pg.RUN_START_REF_ENV, "refs/heads/fix/topic")
    monkeypatch.setenv(pg.RUN_START_TIP_ENV, "a" * 40)
    monkeypatch.setattr(preflight_gate, "check", Mock(return_value=""))
    for name in ("remote_tips", "known_objects", "agent_commit_problems"):
        monkeypatch.setattr(pg, name, Mock(return_value=[]))
    git = Mock(side_effect=[(ancestry, ""), (0, "a" * 40)])
    monkeypatch.setattr(pg, "git", git)
    local, old = shas
    ref = f"refs/heads/{branch}"
    problems = pg.push_problems(
        "origin",
        [f"HEAD {local * 40} {ref} {old * 40}"],
        agent_push=True,
        run_name="example",
    )
    assert problems == ([f"{ref}: {expected}"] if expected else [])
    if ancestry:
        git.assert_called_with(
            *pg.SAFE,
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/remotes/origin/fetched/heads/fix/topic^{commit}",
        )


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/master"])
def test_protected_branch_cannot_be_updated_as_start_ref(ref: str) -> None:
    assert "push to a protected branch refused" in pg.destination_problem(
        ref, deleting=False, agent_push=True, run_name="example", start_ref=ref
    )


@pytest.mark.parametrize(
    "case",
    [
        (True, False, False, "would refuse"),
        (True, True, False, "pass"),
        (True, False, True, "refused"),
        (False, False, False, ""),
    ],
)
def test_push_preflight_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[bool, bool, bool, str],
) -> None:
    agent, recorded, refuse, label = case
    base, pushed, tree = "a" * 40, "b" * 40, "c" * 40
    monkeypatch.setattr(pg, "remote_tips", Mock(return_value=[]))
    monkeypatch.setattr(pg, "ref_problems", Mock(return_value=[]))
    rev_parse = Mock(return_value=(0, f"{tmp_path}\n{tree}"))
    monkeypatch.setattr(preflight_gate, "git", rev_parse)
    monkeypatch.setattr(
        preflight_gate.subprocess, "run", Mock(return_value=Mock(returncode=0))
    )
    if recorded:
        records = tmp_path / "tbhbdev-preflight"
        records.mkdir()
        (records / pushed).write_text(
            f"tree {tree}\nbase {base}\nresult pass\n", encoding="utf-8"
        )
    log = tmp_path / "run.log"
    monkeypatch.setenv("TBHBDEV_RUN_LOG", str(log))
    monkeypatch.setenv(pg.RUN_NAME_ENV, "example")
    monkeypatch.delenv(pg.RUN_START_REF_ENV, raising=False)
    monkeypatch.setattr(pg, "agent_operation", Mock(return_value=agent))
    monkeypatch.setattr(pg, "session_root", Mock(return_value=None))
    monkeypatch.setattr(preflight_gate, "REFUSE", refuse)
    row = f"HEAD^ {pushed} refs/heads/agent/example {base}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(row))

    status = pg.main(["origin", "https://github.com/tbhb-dev/tbhbdev.git"], "guard")
    assert status == int(refuse)
    err = capsys.readouterr().err
    if label:
        assert rev_parse.call_args.args[-1] == f"{pushed}^{{tree}}"
        assert err.startswith(f"push-gate: {label} {pushed}")
        assert log.read_text(encoding="utf-8") == err.splitlines()[0] + "\n"
        assert ("bazelisk run //:preflight" in err) is not recorded
    else:
        rev_parse.assert_not_called()
        assert err == ""
        assert not log.exists()
