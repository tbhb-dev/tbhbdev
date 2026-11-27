# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity validation tests."""

import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from integration.alpha.identity.identity_test_support import (
    ASSIST_OTHER,
    CHILD,
    CLAUDE_COMMIT_ENV,
    CLAUDE_E2E,
    EARLY,
    GEN_ROOT,
    GOOD,
    HUMAN_COMMIT_ENV,
    LAUNCH,
    NO_SESSION_MESSAGE,
    NOREPLY,
    ROOT,
    SESSION_HASH,
    SIGNOFF_BASE,
    claude_turn,
    git_commit,
    last_message,
    msg_file,
    needs_prek,
    run_git,
    seed_claude,
    stage,
    trailers,
)

from tbhbdev.alpha.identity import commit as identity_commit
from tbhbdev.alpha.identity import commit_trailers, storage, validation, values
from tbhbdev.alpha.identity import trailers as identity_trailers
from tbhbdev.alpha.identity.models import EFFORTS

if TYPE_CHECKING:
    from pathlib import Path


def test_llm_trailers_reads_only_llm_trailer_lines() -> None:
    text = (
        "s\n\nGenerated-by: robot\nNote: Generated-by: LLM x=y\n"
        f"{GEN_ROOT[0]}: {GEN_ROOT[1]}\nAssisted-by: LLMX a=b\n"
        f"{ASSIST_OTHER[0]}: {ASSIST_OTHER[1]}\n"
    )
    assert validation.llm_trailers(text) == [GEN_ROOT, ASSIST_OTHER]


@pytest.mark.parametrize("gap", ["", "  ", "\t"])
def test_trailer_lines_read_any_gap_after_the_colon(gap: str) -> None:
    text = f"s\n\nGenerated-by:{gap}{GEN_ROOT[1]}\n"
    assert validation.llm_trailers(text) == [GEN_ROOT]
    assert commit_trailers.LLM_LINES.sub("", text) == "s\n\n"


def test_check_warns_about_a_missing_start_whatever_the_gap(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_claude(repo)
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    path.write_text(
        path.read_text(encoding="utf-8").replace("Generated-by: ", "Generated-by:"),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert validation.cmd_check(str(path), CLAUDE_COMMIT_ENV) == 0
    assert capsys.readouterr() == (
        "",
        "tbhbdev alpha identity: warning: a Generated-by line lacks started_at\n",
    )


# Expected results from the rules trailer grammar at 4b48e96.
TRAILER_VALUE_CASES = [
    ("LLM harness=claude-code;model=anthropic:m", None),
    (
        (
            "LLM effort=max;harness=codex:1;model=openai:gpt-5;"
            f"session={ROOT};subagent={CHILD};surface=cli"
        ),
        None,
    ),
    (f"LLM {GOOD}", None),
    ("LLM harness=antigravity;model=gcp.gemini:m;surface=cli", None),
    ("LLM harness=codex;model=openai:m some prose", None),
    ("LLM prose harness=codex:1:2;model=a=b;surface=c=d more prose", None),
    *[
        (f"LLM effort={effort};harness=codex;model=m", None)
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    ],
    ("", "must start with literal LLM"),
    ("harness=codex", "must start with literal LLM"),
    ("llm harness=codex", "must start with literal LLM"),
    ("LLM", "need exactly one attribute blob"),
    ("LLM a=b c=d", "need exactly one attribute blob"),
    ("LLM harness=codex;", "bad attribute ''"),
    ("LLM harness=codex;Bad=1", "bad attribute 'Bad=1'"),
    ("LLM harness=codex;color=red", "unknown attribute color"),
    ("LLM effort=huge;harness=codex", "bad effort huge"),
    ("LLM harness=codex;session=nope", "bad session hash"),
    (f"LLM harness=codex;session={ROOT};subagent=x", "bad subagent hash"),
    ("LLM harness=cursor", "unknown harness cursor"),
    ("LLM effort=low", "harness required"),
    ("LLM model=a:b;harness=codex", "attributes out of order or duplicated"),
    ("LLM harness=codex;harness=codex", "model required"),
    (
        "LLM harness=codex;harness=codex;model=a:b",
        "attributes out of order or duplicated",
    ),
    ("LLM harness=claude-code", "model required"),
    ("LLM harness=claude-code:2.1;surface=cli", "model required"),
    ("LLM harness=codex:1;surface=cli", "model required"),
    ("LLM harness=antigravity", "model required"),
    ("LLM harness=codex;model=", "bad attribute 'model='"),
    ("LLM harness=codex;model=a,b", "bad attribute 'model=a,b'"),
]


@pytest.mark.parametrize(("value", "problem"), TRAILER_VALUE_CASES)
def test_reference_trailer_value_parity(value: str, problem: str | None) -> None:
    assert validation.check_value(value) == problem
    for key in (identity_trailers.KEY_GEN, identity_trailers.KEY_ASSIST):
        line = f"{key}: {value}"
        assert validation.check_message(line) == (
            [f"{line}: {problem}"] if problem else []
        )


def test_check_pair() -> None:
    assert validation.check_pair("harness=codex") is None
    assert validation.check_pair("surface=a:b/c") is None
    assert validation.check_pair("harness=") == "bad attribute 'harness='"
    assert validation.check_pair("=x") == "bad attribute '=x'"
    assert validation.check_pair("a=b=c") is not None
    for effort in EFFORTS:
        assert validation.check_pair(f"effort={effort}") is None
    assert (
        validation.check_pair("subagent=sha-256-64:ABCDEF0123456789")
        == "bad subagent hash"
    )
    assert validation.check_pair("session=sha-256-64:" + "a" * 17) == "bad session hash"
    assert validation.check_pair("harness=codex:1:2") is None


def test_check_message_orders_and_signoff() -> None:
    gen, assist = f"Generated-by: LLM {GOOD}", f"Assisted-by: LLM {GOOD}"
    assert validation.check_message(f"s\n\n{gen}\n{assist}\n") == []
    assert validation.check_message(f"s\n\n{assist}\n{gen}\n") == [
        f"{gen}: Generated-by must come before Assisted-by"
    ]
    assert validation.check_message(f"s\n\n{gen}\n{gen}\n{assist}\n{assist}\n") == []
    assert validation.check_message("s\n\nGenerated-by: nope\n") == [
        "Generated-by: nope: must start with literal LLM"
    ]
    # Sign-off and co-author trailers are not checked, for anyone.
    assert (
        validation.check_message(
            f"s\n\n{gen}\nSigned-off-by: H <h@x>\nCo-authored-by: C <c@x>\n"
        )
        == []
    )
    assert validation.check_message("Generated-by:LLM harness=codex;model=m\n") == []
    assert validation.check_message("  Generated-by: nope\n") == []
    assert validation.check_message("") == []


@given(
    st.lists(
        st.sampled_from(
            ["effort", "harness", "model", "session", "subagent", "surface"]
        ),
        unique=True,
        min_size=1,
    )
)
def test_check_value_order_property(keys: list[str]) -> None:
    samples = {
        "effort": "low",
        "harness": "codex",
        "model": "openai:m",
        "session": ROOT,
        "subagent": CHILD,
        "surface": "cli",
    }
    ordered = [k for k in values.ATTR_ORDER if k in keys]
    value = "LLM " + ";".join(f"{k}={samples[k]}" for k in ordered)
    expected = None
    if "harness" not in keys:
        expected = "harness required"
    elif "model" not in keys:
        expected = "model required"
    assert validation.check_value(value) == expected
    shuffled = "LLM " + ";".join(f"{k}={samples[k]}" for k in reversed(ordered))
    if len(ordered) > 1 and {"harness", "model"} <= set(keys):
        assert (
            validation.check_value(shuffled) == "attributes out of order or duplicated"
        )


def test_cmd_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = msg_file(tmp_path, f"s\n\nGenerated-by: LLM {GOOD}\n")
    assert validation.cmd_check(str(good), {}) == 0
    assert capsys.readouterr().err == ""
    bad = msg_file(tmp_path, "s\n\nGenerated-by: nope\nSigned-off-by: A <a@x>\n")
    assert validation.cmd_check(str(bad), {}) == 1
    assert capsys.readouterr().err == (
        "tbhbdev alpha identity: Generated-by: nope: must start with literal LLM\n"
    )
    signed = msg_file(
        tmp_path, f"s\n\nGenerated-by: LLM {GOOD}\nSigned-off-by: A <a@x>\n"
    )
    assert validation.cmd_check(str(signed), {}) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("email", ["human@example.invalid", "otheragent@tonyburns.net"])
def test_check_human_without_session_is_unchanged(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    email: str,
) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_EMAIL", email)
    path = msg_file(repo)
    before = path.read_bytes()
    assert validation.cmd_check(str(path), dict(os.environ)) == 0
    assert capsys.readouterr() == ("", "")
    assert path.read_bytes() == before


def test_check_agent_session_keeps_existing_trailer_rules(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_claude(repo)
    path = msg_file(tmp_path)
    assert identity_commit.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    before = path.read_bytes()
    assert b"Generated-by: LLM" in before
    assert validation.cmd_check(str(path), CLAUDE_COMMIT_ENV) == 0
    # A new agent commit that lacks started_at stays valid, with a warning.
    assert capsys.readouterr() == (
        "",
        "tbhbdev alpha identity: warning: a Generated-by line lacks started_at\n",
    )
    assert path.read_bytes() == before
    assert validation.cmd_check(str(path), {}) == 0
    assert capsys.readouterr() == ("", "")
    stamped = before.decode().replace(";surface=", f";started_at={EARLY};surface=")
    assert stamped != before.decode()
    path.write_text(stamped, encoding="utf-8")
    assert validation.cmd_check(str(path), CLAUDE_COMMIT_ENV) == 0
    assert capsys.readouterr() == ("", "")


@needs_prek
def test_e2e_human_commit_is_untouched(wired: Path) -> None:
    stage(wired, "h.txt")
    done = git_commit(wired, "human work", HUMAN_COMMIT_ENV, "-s")
    assert done.returncode == 0, done.stderr
    assert run_git(wired, "log", "-1", "--format=%ae") == "h@example.invalid\n"
    assert "Signed-off-by: Human <h@example.invalid>" in last_message(wired)
    assert trailers(wired) == []


@needs_prek
@pytest.mark.parametrize(
    "roles", [("AUTHOR", "COMMITTER"), ("AUTHOR",), ("COMMITTER",)]
)
def test_e2e_agent_identity_requires_session(
    wired: Path, roles: tuple[str, ...]
) -> None:
    stage(wired, "a.txt")
    identities = {
        f"GIT_{role}_{field}": value
        for role in ("AUTHOR", "COMMITTER")
        for field, value in (
            ("NAME", "tbhb.dev Agent" if role in roles else "Human"),
            ("EMAIL", "agent@tonyburns.net" if role in roles else "h@example.invalid"),
        )
    }
    assert not (wired / ".git" / storage.STORE).exists()
    assert not storage.user_store().exists()
    done = git_commit(wired, "feat: a", identities)
    assert done.returncode != 0
    assert NO_SESSION_MESSAGE in done.stderr.splitlines()
    assert run_git(wired, "log", "-1", "--format=%s") == "init\n"


@needs_prek
def test_e2e_an_agent_signoff_fails_the_check(wired: Path) -> None:
    """`git commit -s` by an agent is rejected; the operator signs off."""
    claude_turn(wired)
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E, "-s")
    assert done.returncode != 0
    assert SIGNOFF_BASE in done.stderr


@needs_prek
def test_e2e_an_agent_commit_as_the_agent_identity_passes(wired: Path) -> None:
    claude_turn(wired)
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    who = run_git(wired, "log", "-1", "--format=%an <%ae>%n%cn <%ce>")
    assert who == f"{validation.AGENT_IDENT}\n{validation.AGENT_IDENT}\n"


@needs_prek
@pytest.mark.parametrize(
    ("flags", "extra", "role", "who"),
    [
        ([f"--author={NOREPLY}"], {}, "author", NOREPLY),
        (
            [],
            {"GIT_COMMITTER_EMAIL": "c@example.invalid"},
            "committer",
            "tbhb.dev Agent <c@example.invalid>",
        ),
    ],
    ids=["noreply-author", "wrong-committer"],
)
def test_e2e_an_agent_commit_with_another_identity_fails(
    wired: Path, flags: list[str], extra: dict[str, str], role: str, who: str
) -> None:
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", {**CLAUDE_E2E, **extra}, *flags)
    assert done.returncode != 0
    assert (
        f"tbhbdev alpha identity: {role} is {who!r}; "
        f"an agent commit must use {validation.AGENT_IDENT}"
    ) in done.stderr
    assert run_git(wired, "log", "-1", "--format=%s") == "init\n"


@needs_prek
def test_e2e_an_agent_amend_keeps_a_wrong_author_and_fails(wired: Path) -> None:
    stage(wired, "a.txt")
    run_git(wired, "commit", "-q", "--no-verify", f"--author={NOREPLY}", "-m", "a")
    done = git_commit(wired, "feat: a", CLAUDE_E2E, "--amend")
    assert done.returncode != 0
    assert f"author is {NOREPLY!r}" in done.stderr


def test_ident_problems_accepts_the_agent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation, "git", agent_git)
    assert validation.ident_problems("", {"CLAUDE_CODE_SESSION_ID": "s"}) == []


@pytest.mark.parametrize("stamp", [" 1794020000 +0000", " 1794020000 -0500"])
def test_ident_date_strips_only_the_timestamp(stamp: str) -> None:
    assert validation.IDENT_DATE.sub("", f"A <a@b> 7 x{stamp}") == "A <a@b> 7 x"


def test_ident_problems_refuses_an_identity_git_cannot_give(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, ...]] = []

    def failing(*args: str, **_: object) -> tuple[int, str]:
        asked.append(args)
        return 128, ""

    monkeypatch.setattr(validation, "git", failing)
    must = f"an agent commit must use {validation.AGENT_IDENT}"
    assert validation.ident_problems("", {"CODEX_SESSION_ID": "s"}) == [
        f"author is ''; {must}",
        f"committer is ''; {must}",
    ]
    assert asked == [("var", "GIT_AUTHOR_IDENT"), ("var", "GIT_COMMITTER_IDENT")]
    assert validation.ident_problems("", {}) == []


def agent_git(*_: str, **__: object) -> tuple[int, str]:
    return 0, f"{validation.AGENT_IDENT} 1794020000 -0500"


@pytest.mark.parametrize(
    "line",
    [
        f"Signed-off-by: {validation.AGENT_IDENT}",
        f"Signed-off-by: {NOREPLY}",
        "Signed-off-by: Human <h@example.invalid>",
        "Co-authored-by: Human <h@example.invalid>",
        "signed-off-by: Human <h@example.invalid>",
        "CO-AUTHORED-BY: Human <h@example.invalid>",
        "Signed-off-by : Human <h@example.invalid>",
    ],
    ids=["agent", "noreply", "human", "co-author", "lowercase", "uppercase", "space"],
)
def test_ident_problems_refuses_a_signoff_or_co_author(
    monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(validation, "git", agent_git)
    text = f"feat: a\n\nbody\n\n{line}\n"
    assert validation.ident_problems(text, {"CODEX_SESSION_ID": "s"}) == [SIGNOFF_BASE]
    assert validation.ident_problems(text, {}) == [validation.SESSION_ERROR]


def test_ident_problems_reports_the_trailers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation, "git", agent_git)
    text = "feat: a\n\nSigned-off-by: A <a@x>\nCo-authored-by: B <b@x>\n"
    assert validation.ident_problems(text, {"CODEX_SESSION_ID": "s"}) == [SIGNOFF_BASE]


@pytest.mark.parametrize(
    "text",
    [
        "feat: a\n",
        "feat: a\n\nGenerated-by: LLM x\n",
        f"feat: a\n\nSee Signed-off-by: {NOREPLY}\n",
    ],
    ids=["none", "other-trailer", "mid-line"],
)
def test_ident_problems_accepts_a_message_without_the_trailers(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr(validation, "git", agent_git)
    assert validation.ident_problems(text, {"CODEX_SESSION_ID": "s"}) == []


def test_ident_problems_ignores_trailers_outside_a_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation, "git", agent_git)
    text = (
        f"feat: a\n\nGenerated-by: LLM {GOOD}\n"
        "Signed-off-by: A <a@x>\nCo-authored-by: B <b@x>\n"
    )
    assert validation.ident_problems(text, {}) == []


def test_cmd_check_reads_the_signoff_from_the_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(validation, "git", agent_git)
    msg = tmp_path / "msg"
    msg.write_text(f"feat: a\n\nSigned-off-by: {NOREPLY}\n")
    assert validation.cmd_check(str(msg), {"CODEX_SESSION_ID": "s"}) == 1
    assert capsys.readouterr().err == f"tbhbdev alpha identity: {SIGNOFF_BASE}\n"


@needs_prek
def test_e2e_malformed_trailer_is_rejected(wired: Path) -> None:
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a\n\nGenerated-by: robot")
    assert done.returncode != 0
    assert "must start with literal LLM" in done.stderr


@needs_prek
def test_e2e_identity_check_fails_even_when_prek_passes(
    with_msg_linter: Path,
) -> None:
    repo = with_msg_linter
    stage(repo, "a.txt")
    done = git_commit(repo, "feat: a", CLAUDE_E2E, "--trailer", "Generated-by: robot")
    assert done.returncode != 0
    assert "must start with literal LLM" in done.stderr
    # prek's hooks still ran after the identity step failed.
    assert (repo / "cog.log").read_text(encoding="utf-8") == "ran\n"


def test_check_pair_splits_at_the_first_equals_sign() -> None:
    assert validation.check_pair("model=a=b") is None
    assert validation.check_pair("model=a=b=c") is None
    assert validation.check_pair("harness=codex:1:2") is None
    assert validation.check_pair("harness=x:codex") == "unknown harness x:codex"


def test_check_value_splits_keys_at_the_first_equals_sign() -> None:
    assert validation.check_value("LLM harness=codex;model=a=b") is None
    assert validation.check_value("LLM harness=codex;model=a=b;surface=c=d") is None
    # Splitting at the last "=" would read these keys as surface=a and
    # surface=b and miss the duplicate.
    assert (
        validation.check_value("LLM harness=codex;model=m;surface=a=1;surface=b=1")
        == "attributes out of order or duplicated"
    )


def test_check_message_orders_attributes_alphabetically() -> None:
    ok = (
        "Generated-by: LLM effort=high;harness=codex;model=openai:m;session="
        + SESSION_HASH
    )
    assert validation.check_value(ok.removeprefix("Generated-by: ")) is None
    swapped = "LLM harness=codex;effort=high;model=m"
    assert validation.check_value(swapped) == "attributes out of order or duplicated"


@pytest.mark.parametrize(
    ("ident", "expected"),
    [
        ("A B <a@b.c> 1700000000 +0000", "a@b.c"),
        ("<x@y.z>", "x@y.z"),
        ("no address", ""),
        ("", ""),
    ],
)
def test_ident_email(ident: str, expected: str) -> None:
    assert validation.ident_email(ident) == expected


def test_check_pair_validates_the_launch_fields() -> None:
    assert validation.check_pair(f"started_at={EARLY}") is None
    assert validation.check_pair(f"launch={LAUNCH}") is None
    assert validation.check_pair("launch=sha-256-64:XYZ") == "bad launch hash"
    bad = "2026-02-30T00:00:00Z"
    assert validation.check_pair(f"started_at={bad}") == f"bad started_at {bad}"
    assert validation.check_pair("started_at=2026-11-14t00:02:41z") is not None
    assert validation.check_pair("Started_at=x") == "bad attribute 'Started_at=x'"
    assert validation.check_pair("a_b=x") == "unknown attribute a_b"
