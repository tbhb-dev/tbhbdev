# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Identity commit trailers tests."""

import dataclasses
from typing import TYPE_CHECKING

import pytest
from integration.alpha.identity.identity_test_support import (
    ASSIST_OTHER,
    CHILD,
    CLAUDE_COMMIT_ENV,
    EARLY,
    GEN_ROOT,
    GEN_SUB,
    LATE,
    LAUNCH,
    OTHER,
    ROOT,
    H,
    init_repo,
    line_of,
    make_ident,
    msg_file,
    returning,
    run_git,
)

from tbhbdev.alpha.identity import (
    claude_commands,
    commit_trailers,
    replay,
    sessions,
    validation,
    values,
)
from tbhbdev.alpha.identity import commit as identity_commit
from tbhbdev.alpha.identity import trailers as identity_trailers

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.usefixtures("repo")
def test_add_trailers_formats_and_dedupes(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    gen = make_ident(CHILD, "anthropic:m", ROOT, "low", "cli")
    assisted = make_ident(model=None)
    commit_trailers.add_trailers(path, [gen, gen], [assisted])
    text = path.read_text(encoding="utf-8")
    assert text == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=low;harness=claude-code;model=anthropic:m;"
        f"session={ROOT};subagent={CHILD};surface=cli\n"
        f"Assisted-by: LLM harness=claude-code;session={ROOT}\n"
    )
    assert text.count("Generated-by:") == 1
    assert text.index("Generated-by:") < text.index("Assisted-by:")
    commit_trailers.add_trailers(path, [gen], [assisted])
    assert path.read_text(encoding="utf-8").count("Generated-by:") == 1


def test_add_trailers_leaves_the_file_when_git_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = msg_file(tmp_path)
    monkeypatch.setattr(commit_trailers, "git", returning((1, "garbage")))
    commit_trailers.add_trailers(path, [make_ident()], [])
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"


@pytest.mark.usefixtures("repo")
def test_add_trailers_keeps_a_human_signoff_in_place(tmp_path: Path) -> None:
    path = msg_file(tmp_path, "subject\n\nSigned-off-by: Human <h@example.invalid>\n")
    commit_trailers.add_trailers(path, [make_ident()], [])
    text = path.read_text(encoding="utf-8")
    assert text.index("Signed-off-by") < text.index("Generated-by")


ASSIST_ROOT = ("Assisted-by", f"LLM harness=claude-code;session={ROOT}")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"LLM harness=x;session={ROOT};subagent={CHILD}", (ROOT, CHILD)),
        (f"LLM session={ROOT}", (ROOT, None)),
        (f"LLM xsession={ROOT};model=a:subagent=b", (None, None)),
        ("LLM", (None, None)),
    ],
)
def test_trailer_agent(value: str, expected: tuple[str | None, str | None]) -> None:
    assert commit_trailers.trailer_agent(value) == expected


def test_merge_trailers_dedupes_orders_and_lets_generated_win() -> None:
    found = [ASSIST_ROOT, ASSIST_OTHER, GEN_SUB, GEN_ROOT, GEN_SUB, ASSIST_OTHER]
    assert commit_trailers.merge_trailers(found) == [GEN_SUB, GEN_ROOT, ASSIST_OTHER]


def test_add_trailers_rewrites_existing_ones_in_order(tmp_path: Path) -> None:
    path = msg_file(
        tmp_path,
        f"s\n\nbody\n\n{ASSIST_ROOT[0]}: {ASSIST_ROOT[1]}\nGenerated-by: robot\n",
    )
    commit_trailers.add_trailers(path, [make_ident(model=None)], [], [GEN_SUB])
    assert path.read_text(encoding="utf-8") == (
        "s\n\nbody\n\nGenerated-by: robot\n"
        f"{GEN_SUB[0]}: {GEN_SUB[1]}\n{GEN_ROOT[0]}: {GEN_ROOT[1]}\n"
    )


def test_add_trailers_asks_git_to_add_only_different_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def spy(*args: str, **_kwargs: Path | None) -> tuple[int, str]:
        calls.append(args)
        return 1, ""

    monkeypatch.setattr(commit_trailers, "git", spy)
    path = msg_file(tmp_path)
    commit_trailers.add_trailers(
        path, [make_ident(model=None)], [make_ident(model=None, root=OTHER)]
    )
    assert [call for call in calls if call[0] == "interpret-trailers"] == [
        (
            "interpret-trailers",
            "--if-exists",
            "addIfDifferent",
            f"--trailer=Generated-by: LLM harness=claude-code;session={ROOT}",
            f"--trailer=Assisted-by: LLM harness=claude-code;session={OTHER}",
            str(path.with_name("MSG.ascm")),
        )
    ]
    assert not path.with_name("MSG.ascm").exists()


@pytest.mark.usefixtures("repo")
def test_with_start_reads_the_record_and_the_roots_launch() -> None:
    root, sub = make_ident(), make_ident(CHILD)
    assert commit_trailers.with_start(root) == root
    sessions.write_record(ROOT, {}, once={"started_at": EARLY, "launch": LAUNCH})
    assert commit_trailers.with_start(root) == dataclasses.replace(
        root, launch=LAUNCH, started_at=EARLY
    )
    assert commit_trailers.with_start(sub) == dataclasses.replace(sub, launch=LAUNCH)
    sessions.write_record(CHILD, {"root": ROOT}, once={"started_at": LATE})
    assert commit_trailers.with_start(sub) == dataclasses.replace(
        sub, launch=LAUNCH, started_at=LATE
    )
    sessions.write_record(CHILD, {"launch": OTHER, "started_at": "bad"})
    sessions.write_record(ROOT, {"launch": "bad"})
    assert commit_trailers.with_start(sub) == dataclasses.replace(sub, launch=OTHER)
    assert commit_trailers.with_start(root) == dataclasses.replace(
        root, started_at=EARLY
    )
    sessions.write_record(ROOT, {"launch": LAUNCH})
    sessions.write_record(CHILD, {"launch": "bad"})
    assert commit_trailers.with_start(sub) == dataclasses.replace(sub, launch=LAUNCH)
    value = values.format_value(
        dataclasses.replace(sub, launch=LAUNCH, started_at=EARLY).attrs()
    )
    assert value == (
        f"LLM harness=claude-code;launch={LAUNCH};model=anthropic:m;"
        f"session={ROOT};started_at={EARLY};subagent={CHILD}"
    )
    assert validation.check_value(value) is None


def test_without_start_and_trailer_start() -> None:
    value = f"LLM harness=h;model=m;session={ROOT};started_at={EARLY};subagent={CHILD}"
    assert (
        commit_trailers.without_start(value)
        == f"LLM harness=h;model=m;session={ROOT};subagent={CHILD}"
    )
    assert (
        commit_trailers.without_start(f"LLM x_started_at={EARLY}")
        == f"LLM x_started_at={EARLY}"
    )
    assert identity_trailers.trailer_start(value) == EARLY
    assert identity_trailers.trailer_start(f"LLM x_started_at={EARLY}") == ""
    assert identity_trailers.trailer_start("LLM harness=h") == ""
    assert identity_trailers.trailer_start("LLM started_at=0") == ""
    assert identity_trailers.trailer_start("LLM started_at=2026-02-30T00:00:00Z") == ""


def test_merge_trailers_keeps_the_earliest_start_per_identity() -> None:
    gen, assist = "Generated-by", "Assisted-by"
    found = [
        (gen, line_of()),
        (gen, line_of(LATE)),
        (gen, line_of(EARLY, "effort=low;")),
        (gen, line_of(EARLY)),
        (gen, line_of(LATE, who=OTHER)),
        (gen, line_of(who=CHILD)),
        (gen, line_of(extra="effort=low;", who=CHILD)),
        (assist, line_of(LATE, who=H + "e" * 16)),
        (assist, line_of(EARLY, who=H + "e" * 16)),
    ]
    assert commit_trailers.merge_trailers(found) == [
        (gen, line_of(EARLY)),
        (gen, line_of(EARLY, "effort=low;")),
        (gen, line_of(LATE, who=OTHER)),
        (gen, line_of(who=CHILD)),
        (gen, line_of(extra="effort=low;", who=CHILD)),
        (assist, line_of(EARLY, who=H + "e" * 16)),
    ]
    # The key is part of the identity, and each attribute of it counts.
    apart = [
        (gen, f"LLM harness=a;model=m;session={ROOT};started_at={LATE}"),
        (gen, f"LLM harness=b;model=m;session={ROOT};started_at={EARLY}"),
        (gen, f"LLM harness=a;model=n;session={ROOT};started_at={EARLY}"),
        (
            gen,
            f"LLM harness=a;model=m;session={ROOT};started_at={EARLY};subagent={CHILD}",
        ),
    ]
    assert commit_trailers.earliest_starts(apart) == apart
    # A line that differs in any other attribute never takes another's place,
    # so a hand-written earlier time cannot drop the hook's launch or effort.
    real = (gen, line_of(LATE, f"launch={LAUNCH};"))
    forged = (gen, line_of(EARLY))
    assert commit_trailers.merge_trailers([real, forged]) == [real, forged]
    mixed = [(assist, line_of(EARLY)), (gen, line_of(LATE, who=OTHER))]
    assert commit_trailers.earliest_starts(mixed) == mixed


@pytest.mark.usefixtures("repo")
def test_with_record_drops_a_launch_the_record_lacks() -> None:
    session = values.sha256_64("session-raw")
    sessions.write_record(session, {}, once={"started_at": LATE})
    forged = (
        f"LLM harness=claude-code;launch={LAUNCH};model=m;session={session};"
        f"started_at={EARLY}"
    )
    stamped = f"LLM harness=claude-code;model=m;session={session};started_at={LATE}"
    assert commit_trailers.with_record(forged) == stamped


@pytest.mark.usefixtures("repo")
def test_with_record_overrides_a_forged_line() -> None:
    session = values.sha256_64("session-raw")
    sessions.write_record(session, {}, once={"started_at": LATE, "launch": LAUNCH})
    forged = (
        f"LLM harness=claude-code;launch=sha-256-64:{'f' * 16};"
        f"model=m;session={session};started_at={EARLY}"
    )
    stamped = (
        f"LLM harness=claude-code;launch={LAUNCH};model=m;session={session};"
        f"started_at={LATE}"
    )
    assert commit_trailers.with_record(forged) == stamped
    assert commit_trailers.with_record(stamped) == stamped
    other = f"LLM harness=claude-code;model=m;session={OTHER};started_at={EARLY}"
    assert commit_trailers.with_record(other) == other
    assert (
        commit_trailers.with_record("LLM harness=claude-code;model=m")
        == "LLM harness=claude-code;model=m"
    )


@pytest.mark.usefixtures("repo")
@pytest.mark.parametrize("launch", [None, LAUNCH])
def test_with_record_drops_a_nonexistent_child_forged_start(
    launch: str | None,
) -> None:
    sessions.write_record(ROOT, {"started_at": LATE, "launch": launch})
    base = f"LLM harness=claude-code;model=m;session={ROOT}"
    value = f"{base};started_at={EARLY};subagent={CHILD}"
    expected = (
        base if launch is None else base.replace(";model=", f";launch={launch};model=")
    )
    assert commit_trailers.with_record(value) == f"{expected};subagent={CHILD}"


@pytest.mark.usefixtures("repo")
@pytest.mark.parametrize(
    "value",
    [
        f"LLM harness=claude-code;model=m;session={ROOT};unknown=x",
        f"LLM harness=claude-code;model=m;model=n;session={ROOT}",
        f"LLM model=m;harness=claude-code;session={ROOT}",
    ],
)
def test_with_record_does_not_repair_invalid_attributes(
    tmp_path: Path, value: str
) -> None:
    sessions.write_record(ROOT, {"started_at": EARLY, "launch": LAUNCH})
    problem = validation.check_value(value)
    assert problem is not None
    path = msg_file(tmp_path, f"subject\n\nGenerated-by: {value}\n")
    commit_trailers.add_trailers(path, [], [])
    assert validation.llm_trailers(path.read_text()) == [
        (identity_trailers.KEY_GEN, value)
    ]
    assert validation.check_message(path.read_text()) == [
        f"Generated-by: {value}: {problem}"
    ]


@pytest.mark.usefixtures("repo")
def test_with_record_preserves_prose_and_spacing() -> None:
    sessions.write_record(ROOT, {"started_at": EARLY, "launch": LAUNCH})
    blob = f"harness=claude-code;model=m;session={ROOT}"
    value = f"LLM  prose\t{blob}  more prose"
    stamped = (
        f"harness=claude-code;launch={LAUNCH};model=m;session={ROOT};started_at={EARLY}"
    )
    assert validation.check_value(value) is None
    assert commit_trailers.with_record(value) == value.replace(blob, stamped)
    assert commit_trailers.with_record(value.replace(blob, stamped)) == value.replace(
        blob, stamped
    )


@pytest.mark.parametrize("child_launch", [None, OTHER])
def test_with_record_reads_registered_child_fields(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_launch: str | None,
) -> None:
    other = init_repo(tmp_path / "other")
    monkeypatch.setattr(sessions, "write_user_record", returning(None))
    sessions.write_record(ROOT, {"started_at": LATE, "launch": LAUNCH}, other)
    sessions.write_record(
        CHILD, {"root": ROOT, "started_at": EARLY, "launch": child_launch}, other
    )
    claude_commands.record_command(ROOT, CHILD, "ls", other)
    assert sessions.load_record(CHILD, repo) == {}
    value = f"LLM harness=claude-code;model=m;session={ROOT};subagent={CHILD}"
    launch = child_launch or LAUNCH
    assert commit_trailers.with_record(value) == (
        f"LLM harness=claude-code;launch={launch};model=m;session={ROOT};"
        f"started_at={EARLY};subagent={CHILD}"
    )


@pytest.mark.usefixtures("repo")
@pytest.mark.parametrize("attribute", ["harness", "model"])
@pytest.mark.parametrize("subagent", [None, CHILD])
def test_add_trailers_replaces_own_forged_harness_or_model(
    tmp_path: Path, attribute: str, subagent: str | None
) -> None:
    real = make_ident(subagent)
    forged = values.format_value(
        {
            **real.attrs(),
            attribute: "codex" if attribute == "harness" else "openai:forged",
        }
    )
    assert validation.check_value(forged) is None
    path = msg_file(tmp_path, f"subject\n\nGenerated-by: {forged}\n")
    commit_trailers.add_trailers(path, [real], [])
    assert validation.llm_trailers(path.read_text()) == [
        (identity_trailers.KEY_GEN, values.format_value(real.attrs()))
    ]


@pytest.mark.usefixtures("repo")
@pytest.mark.parametrize("agent_first", [True, False])
def test_cmd_trailer_squash_of_agent_and_human_moves_single_trailer(
    tmp_path: Path, agent_first: bool
) -> None:
    agent = f"agent\n\nGenerated-by: {line_of(EARLY)}\n"
    human = "human\n\nA human body.\n"
    first, second = (agent, human) if agent_first else (human, agent)
    joined = (
        "# This is a combination of 2 commits.\n"
        f"{first}\n# This is the commit message #2:\n\n{second}"
    )
    path = msg_file(tmp_path, joined)
    assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
    text = path.read_text()
    assert text.rstrip().endswith(f"Generated-by: {line_of(EARLY)}")
    assert "A human body." in text.split("Generated-by:")[0]


def test_cmd_trailer_interactive_squash_of_two_sessions_moves_every_line(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".git" / "rebase-merge").mkdir()
    monkeypatch.setattr(replay, "ancestry", returning(["git rebase -i main"]))
    one, two = line_of(LATE), line_of(EARLY, who=OTHER)
    joined = (
        "# This is a combination of 2 commits.\n# This is the 1st commit message:\n\n"
        f"one\n\nGenerated-by: {one}\n\n"
        "# This is the commit message #2:\n\n"
        f"two\n\nGenerated-by: {two}\n"
    )
    envs: tuple[dict[str, str], ...] = ({}, CLAUDE_COMMIT_ENV)
    for env in envs:
        path = msg_file(tmp_path, joined)
        assert identity_commit.cmd_trailer(str(path), "message", env) == 0
        text = path.read_text(encoding="utf-8")
        assert validation.llm_trailers(text) == [
            ("Generated-by", one),
            ("Generated-by", two),
        ]
        assert text.rstrip().endswith(f"Generated-by: {one}\nGenerated-by: {two}")
    # A message with no squash marker keeps lines where its author put them.
    plain = f"one\n\nGenerated-by: {one}\n\ntwo\n\nGenerated-by: {two}\n"
    path = msg_file(tmp_path, plain)
    before = path.read_bytes()
    assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("comment_char", [None, ";", "auto", "$"])
def test_cmd_trailer_interactive_squash_respects_comment_char(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    comment_char: str | None,
) -> None:
    """Distinct sessions with identical starts still move into one trailer block."""
    if comment_char:
        run_git(repo, "config", "core.commentChar", comment_char)
    # Git validates the configuration, including support for the auto value.
    run_git(repo, "var", "GIT_COMMITTER_IDENT")
    monkeypatch.setattr(replay, "ancestry", returning(["git rebase -i main"]))
    marker = ";" if comment_char == "auto" else (comment_char or "#")
    one, two = line_of(EARLY), line_of(EARLY, who=OTHER)
    joined = (
        "An introductory line.\n"
        f"{marker} This is a combination of 2 commits.\n"
        f"{marker} This is the 1st commit message:\n\n"
        f"one\n\n# A body line makes auto choose another comment character.\n\n"
        f"Generated-by: {one}\n\n"
        f"{marker} This is the commit message #2:\n\n"
        f"two\n\nGenerated-by: {two}\n"
    )
    path = msg_file(tmp_path, joined)
    assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
    text = path.read_text(encoding="utf-8")
    assert validation.llm_trailers(text) == [
        ("Generated-by", one),
        ("Generated-by", two),
    ]
    assert text.rstrip().endswith(f"Generated-by: {one}\nGenerated-by: {two}")
    # Neither an unconfigured prefix nor an inline quotation is a squash marker.
    for header in (
        "~ This is a combination of 2 commits.",
        "X This is a combination of 2 commits.",
        f"{';' if marker == '#' else '#'} This is a combination of 2 commits."
        if comment_char != "auto"
        else "? This is a combination of 2 commits.",
        f"Quoted: {marker} This is a combination of 2 commits.",
    ):
        plain = f"{header}\n\n{joined.split('one', 1)[1]}"
        path = msg_file(tmp_path, plain)
        assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
        assert path.read_text(encoding="utf-8") == plain


def test_cmd_trailer_interactive_squash_keeps_the_earliest_start(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git joins the squashed commits' messages, and the hook merges their lines."""
    (repo / ".git" / "rebase-merge").mkdir()
    monkeypatch.setattr(replay, "ancestry", returning(["git rebase -i main"]))
    joined = (
        "# This is a combination of 2 commits.\n# This is the 1st commit message:\n\n"
        f"one\n\nGenerated-by: {line_of(LATE)}\n\n"
        "# This is the commit message #2:\n\n"
        f"two\n\nGenerated-by: {line_of(EARLY)}\nGenerated-by: {line_of()}\n"
    )
    envs: tuple[dict[str, str], ...] = ({}, CLAUDE_COMMIT_ENV)
    for env in envs:
        path = msg_file(tmp_path, joined)
        assert identity_commit.cmd_trailer(str(path), "message", env) == 0
        assert validation.llm_trailers(path.read_text(encoding="utf-8")) == [
            ("Generated-by", line_of(EARLY))
        ]
    # A human's message whose lines need no merge stays byte for byte.
    path = msg_file(tmp_path, f"one\n\n\nGenerated-by: {line_of(LATE)}\n")
    before = path.read_bytes()
    assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
    assert path.read_bytes() == before
    # So does one with repeated or unordered lines that no start time separates.
    path = msg_file(
        tmp_path,
        f"one\n\nAssisted-by: {line_of(LATE)}\nGenerated-by: {line_of(LATE)}\n"
        f"Generated-by: {line_of(LATE)}\n",
    )
    before = path.read_bytes()
    assert identity_commit.cmd_trailer(str(path), "message", {}) == 0
    assert path.read_bytes() == before
