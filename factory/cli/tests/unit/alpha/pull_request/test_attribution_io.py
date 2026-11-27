# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request attribution."""

import json
import re
import subprocess
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_draft_samples import (
    BASE,
    FALLBACK,
    GEN,
    GEN_LINE,
    OTHER,
)
from unit.alpha.pull_request.pr_test_support import (
    OPENER,
    POSTING,
    PR_COMMITS_CALL,
    TARGET,
    World,
    ledgers,
    pr_page,
    run_main,
    seed_session,
    view_case,
    write_draft,
)

from tbhbdev.alpha.identity import dispatcher as agent_identity
from tbhbdev.alpha.identity.trailers import merge_pr_trailers
from tbhbdev.alpha.pull_request import attribution as pr_attribution
from tbhbdev.alpha.pull_request import draft as pr_draft
from tbhbdev.alpha.pull_request import draft_io as pr_draft_io
from tbhbdev.alpha.pull_request import tools as pr_tools
from tbhbdev.alpha.pull_request.draft import DRAFT

if TYPE_CHECKING:
    from pathlib import Path


agent = pr_test_support.agent
host_names = pr_test_support.host_names
world = pr_test_support.world


def test_posting_value(world: World, agent: dict[str, str]) -> None:
    del world
    assert pr_attribution.posting_value(agent) == POSTING
    assert pr_attribution.posting_value({}) is None


def test_branch_trailers_are_in_commit_order(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}\nAssisted-by: {OTHER}")
    world.commit("plain")
    world.commit(f"three\n\nSigned-off-by: T <t@example.com>\nGenerated-by: {OTHER}")
    assert pr_attribution.branch_trailers("main") == [
        ("Generated-by", GEN),
        ("Assisted-by", OTHER),
        ("Generated-by", OTHER),
    ]


def test_branch_trailers_stop_at_the_merge_base(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("switch", "-q", "main")
    world.commit(f"theirs\n\nGenerated-by: {OTHER}")
    world.git("switch", "-q", "topic")
    assert pr_attribution.branch_trailers("main") == [("Generated-by", GEN)]
    assert pr_attribution.branch_trailers("topic") == []


def test_branch_trailers_prefer_the_remote_base(world: World) -> None:
    world.commit(f"upstream\n\nGenerated-by: {OTHER}")
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("branch", "-f", "main", "HEAD~2")
    with patch.object(pr_tools, "git", wraps=pr_tools.git) as git:
        assert pr_attribution.branch_trailers("main") == [("Generated-by", GEN)]
    git.assert_any_call(
        "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main^{commit}"
    )


def test_branch_trailers_ignore_a_tag_named_like_the_remote_base(
    world: World,
) -> None:
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("tag", "origin/main")
    assert pr_attribution.branch_trailers("main") == [("Generated-by", GEN)]


def test_branch_trailers_need_a_known_base(world: World) -> None:
    del world
    with pytest.raises(
        SystemExit, match=r"cannot list the commits in nope\.\.HEAD; is nope fetched"
    ):
        pr_attribution.branch_trailers("nope")


def checked_for() -> pr_draft.Checked:
    return pr_draft_io.check_text(BASE)


def test_description_without_trailers_is_the_body(world: World) -> None:
    del world
    got = pr_attribution.description(checked_for(), {}, [])
    assert got == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"


def test_description_appends_a_trailer_paragraph(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    got = pr_attribution.description(
        checked_for(), agent, pr_attribution.branch_trailers("main")
    )
    assert got.endswith(f"\nv\n\nGenerated-by: {GEN}\nAssisted-by: {POSTING}\n")
    parsed = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=got,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert parsed == f"Generated-by: {GEN}\nAssisted-by: {POSTING}\n"
    assert "Signed-off-by" not in got
    assert "raw-session" not in got


def test_human_posting_adds_only_branch_trailers(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    assert pr_attribution.description(
        checked_for(), {}, pr_attribution.branch_trailers("main")
    ).endswith("v\n")
    world.commit(f"one\n\nAssisted-by: {GEN}")
    got = pr_attribution.description(
        checked_for(), {}, pr_attribution.branch_trailers("main")
    )
    assert got.endswith(f"v\n\nAssisted-by: {GEN}\n")
    assert got.count("Assisted-by") == 1


def test_posting_agent_already_on_the_branch_is_not_repeated(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {POSTING}")
    got = pr_attribution.description(
        checked_for(), agent, pr_attribution.branch_trailers("main")
    )
    assert got.endswith(f"v\n\nGenerated-by: {POSTING}\n")


def test_a_resolved_posting_value_is_one_line_with_its_commit_trailer(
    world: World, agent: dict[str, str]
) -> None:
    """The session's posting value has model=, so it matches its own commit."""
    assert "model=anthropic:claude-opus-5-5" in POSTING
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {POSTING}")
    got = pr_attribution.description(
        checked_for(), agent, pr_attribution.branch_trailers("main")
    )
    session = f"session={agent_identity.sha256_64('raw-session')}"
    lines = [line for line in got.splitlines() if session in line]
    assert lines == [f"Generated-by: {POSTING}"]
    assert "Assisted-by" not in got
    assert pr_attribution.posting_value(agent) == POSTING


def test_the_posting_session_dedupes_against_its_commit_trailer(
    world: World, agent: dict[str, str]
) -> None:
    """Regression: a posting value without model= added a second line."""
    write_draft(world)
    world.git("switch", "-q", "-c", "topic")
    # The trailer the session's commit got, from the same commit-time source.
    committed = agent_identity.committer_identity(agent, [])
    assert committed is not None
    trailer = agent_identity.format_value(committed.ident.attrs())
    assert "model=anthropic:claude-opus-5-5" in trailer
    world.commit(f"one\n\nGenerated-by: {trailer}")
    assert pr_attribution.posting_value(agent) == trailer
    merged = merge_pr_trailers(
        pr_attribution.branch_trailers("main"), pr_attribution.posting_value(agent)
    )
    assert merged == [("Generated-by", trailer)]
    assert run_main("create", env=agent) == 0
    _, call = world.calls("gh")
    session = f"session={agent_identity.sha256_64('raw-session')}"
    lines = [line for line in str(call["stdin"]).splitlines() if session in line]
    assert lines == [OPENER, f"Generated-by: {trailer}"]


@pytest.mark.parametrize("argv", [["create"], ["edit", str(DRAFT), "--pr", "7"]])
def test_an_unresolved_posting_model_fails_without_posting(
    world: World, agent: dict[str, str], argv: list[str]
) -> None:
    write_draft(world)
    seed_session(world, None)
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    posting = POSTING.replace("model=anthropic:claude-opus-5-5;", "")
    with pytest.raises(SystemExit) as info:
        run_main(*argv, env=agent)
    assert info.value.code == f"invalid LLM trailer: {posting}: model required"
    reads = (
        [
            ["pr", "view", "7", "--json", "body,url"],
            [*PR_COMMITS_CALL, f"repos/{TARGET}/pulls/7/commits"],
        ]
        if argv[0] == "edit"
        else []
    )
    assert [call["argv"] for call in world.calls("gh")] == reads


def test_description_uses_the_frontmatter_base(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    world.git("branch", "dev", "main")
    world.git("switch", "-q", "dev")
    world.commit(f"dev\n\nGenerated-by: {OTHER}")
    world.git("update-ref", "refs/remotes/origin/dev", "HEAD")
    world.git("switch", "-q", "-c", "topic2")
    world.commit(f"two\n\nAssisted-by: {GEN}")
    write_draft(world, "---\nbase: dev\n---\n" + BASE)
    assert run_main("create") == 0
    _, call = world.calls("gh")
    got = str(call["stdin"])
    assert got.endswith(f"v\n\nAssisted-by: {GEN}\n")
    assert OTHER not in got


@pytest.mark.parametrize(
    "bad", ["LLM model=a:m", "claude-code:opus", "LLM harness=nope"]
)
def test_invalid_branch_trailers_stop_the_description(world: World, bad: str) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {bad}")
    with pytest.raises(SystemExit, match=r"invalid LLM trailer: .*"):
        pr_attribution.description(
            checked_for(), {}, pr_attribution.branch_trailers("main")
        )


def test_invalid_trailer_message_names_the_value(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit("one\n\nGenerated-by: LLM model=a:m")
    with pytest.raises(SystemExit) as info:
        pr_attribution.description(
            checked_for(), {}, pr_attribution.branch_trailers("main")
        )
    assert str(info.value) == "invalid LLM trailer: LLM model=a:m: harness required"


def fake_pr_commits(
    world: World, number: int, stdout: str, *, rc: int = 0, stderr: str = ""
) -> list[str]:
    argv = [*PR_COMMITS_CALL, f"repos/{TARGET}/pulls/{number}/commits"]
    answer: dict[str, object] = {"rc": rc, "stdout": stdout, "stderr": stderr}
    view = view_case(pr_page(TARGET, number))
    world.fake("gh", cases={"pr view": view, " ".join(argv): answer})
    return argv


def test_edit_pr_40_uses_github_commit_trailers(
    world: World, agent: dict[str, str]
) -> None:
    local = (
        "LLM effort=medium;harness=codex:0.171.0;model=openai:gpt-6.2-sol;"
        "session=sha-256-64:ae40cbadd8ab72ec;surface=cli"
    )
    attrs = (
        "effort=medium;harness=claude-code;model=anthropic:claude-sonnet-5-5;"
        "session=sha-256-64:7ff1a5b545107a55;surface=cli"
    )
    merged = attrs.replace("7ff1a5b545107a55", "1234567890abcdef")
    world.git("switch", "-q", "-c", "other-branch")
    world.commit(f"other PR\n\nGenerated-by: {local}")
    pages = [
        [{"commit": {"message": f"one\n\nGenerated-by: LLM {attrs}"}}],
        [
            {"commit": {"message": f"two\n\nGenerated-by: LLM {attrs}"}},
            {
                "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}],
                "commit": {"message": f"merge\n\n---\n\nGenerated-by: LLM\n {merged}"},
            },
            {"commit": {"message": f"body\n\nGenerated-by: {local}\n\nMore prose."}},
        ],
    ]
    argv = fake_pr_commits(world, 40, json.dumps(pages))
    write_draft(world)
    assert run_main("edit", str(DRAFT), "--pr", "40", env=agent) == 0
    _, commits, edit = world.calls("gh")
    assert commits["argv"] == argv
    assert str(edit["stdin"]).split("\n\n")[-1] == (
        f"Generated-by: LLM {attrs}\nGenerated-by: LLM {merged}\n"
        f"Assisted-by: {POSTING}\n"
    )
    (scan,) = world.calls("gitleaks")
    assert scan["stdin"] == f"fix: x\n\n{edit['stdin']}"


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr", "why"),
    [
        (1, "[[{}]]", "no\naccess", "no access"),
        (0, "", "", "GitHub returned no commit list"),
        (0, "[[]]", "", "GitHub returned no commits"),
        (0, "[{}]", "", "GitHub returned invalid commit data"),
        (0, "[[1]]", "", "GitHub returned invalid commit data"),
        (0, '[[{"commit":{"message":1}}]]', "", "GitHub returned invalid commit data"),
    ],
)
def test_edit_refuses_unavailable_commits(
    world: World, rc: int, stdout: str, stderr: str, why: str
) -> None:
    argv = fake_pr_commits(world, 7, stdout, rc=rc, stderr=stderr)
    draft = write_draft(world)
    message = f"cannot list the commits of PR 7: {why}"
    with pytest.raises(SystemExit, match=f"^{re.escape(message)}$"):
        run_main("edit", str(DRAFT), "--pr", "7")
    assert draft.read_text() == BASE
    assert [call["argv"] for call in world.calls("gh")] == [
        ["pr", "view", "7", "--json", "body,url"],
        argv,
    ]
    assert world.calls("gitleaks") == []


def test_edit_refuses_unreadable_trailers(world: World) -> None:
    world.fake("git", rc=1)
    write_draft(world)
    with pytest.raises(SystemExit, match=r"^cannot parse the commit trailers of PR 7$"):
        run_main("edit", str(DRAFT), "--pr", "7")
    assert len(world.calls("gh")) == 2


def test_posting_agent_is_added_to_an_empty_branch(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    got = pr_attribution.description(
        checked_for(), agent, pr_attribution.branch_trailers("main")
    )
    assert got.endswith(f"v\n\nAssisted-by: {POSTING}\n")


def test_a_posting_value_without_a_model_is_rejected(
    world: World, agent: dict[str, str]
) -> None:
    seed_session(world, None)
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    posting = POSTING.replace("model=anthropic:claude-opus-5-5;", "")
    assert pr_attribution.posting_value(agent) == posting
    with pytest.raises(SystemExit) as info:
        pr_attribution.description(
            checked_for(), agent, pr_attribution.branch_trailers("main")
        )
    assert str(info.value) == f"invalid LLM trailer: {posting}: model required"


def test_a_bad_branch_trailer_and_posting_value_are_both_listed(
    world: World, agent: dict[str, str]
) -> None:
    seed_session(world, None)
    world.git("switch", "-q", "-c", "topic")
    world.commit("one\n\nGenerated-by: LLM harness=nope")
    posting = POSTING.replace("model=anthropic:claude-opus-5-5;", "")
    with pytest.raises(SystemExit) as info:
        pr_attribution.description(
            checked_for(), agent, pr_attribution.branch_trailers("main")
        )
    assert str(info.value) == (
        "invalid LLM trailer: LLM harness=nope: unknown harness nope; "
        f"{posting}: model required"
    )


def test_every_invalid_trailer_is_listed(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(
        "one\n\nGenerated-by: LLM harness=nope\nAssisted-by: LLM harness=nope2"
    )
    with pytest.raises(SystemExit) as info:
        pr_attribution.description(
            checked_for(), {}, pr_attribution.branch_trailers("main")
        )
    assert "; " in str(info.value)
    assert str(info.value).count("LLM harness=") == 2


@pytest.mark.usefixtures("world")
def test_branch_git_calls_run_in_the_current_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path | None] = []
    real = pr_tools.git

    def spy(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        seen.append(cwd)
        return real(*args, cwd=cwd)

    monkeypatch.setattr(pr_tools, "git", spy)
    pr_attribution.branch_trailers("main")
    assert seen == [None, None]


@pytest.mark.usefixtures("world")
def test_branch_trailers_come_from_the_current_repository_not_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trailers come from the repository the command runs in."""
    other = tmp_path / "other"
    other.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "commit.gpgsign", "false"),
        ("commit", "--allow-empty", "-q", "-m", "init"),
        ("update-ref", "refs/remotes/origin/main", "HEAD"),
        ("commit", "--allow-empty", "-q", "-m", f"feat: x\n\nGenerated-by: {FALLBACK}"),
    ):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    monkeypatch.chdir(other)
    assert pr_tools.root() == other.resolve()
    assert pr_attribution.branch_trailers("main") == [("Generated-by", FALLBACK)]


def test_description_for_one_identity_ends_in_one_generated_by(
    world: World, agent: dict[str, str]
) -> None:
    """Regression: a branch commit and the posting agent share an identity."""
    posting = pr_attribution.posting_value(agent)
    assert posting is not None
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"feat: x\n\nGenerated-by: {posting}")
    checked = pr_draft_io.check_text(BASE)
    body = pr_attribution.description(
        checked, agent, pr_attribution.branch_trailers("main")
    )
    assert body.endswith(f"\nGenerated-by: {posting}\n")
    assert "Assisted-by" not in body


LIVE = "Opened-by: LLM harness=codex;session=sha-256-64:cccccccccccccccc"


def live_body(body: object, rc: int = 0, stderr: str = "") -> dict[str, object]:
    """The fake gh answer to ``gh pr view N --json body,url``."""
    if rc == 0:
        return view_case(pr_page(TARGET), body)
    return {"rc": rc, "stdout": "", "stderr": stderr}


@pytest.mark.parametrize(
    ("body", "kept"),
    [
        (f"## Why\n\nw\n\n{LIVE}\n{GEN_LINE}\n", LIVE),
        (f"## Why\r\n\r\nw\r\n\r\n{LIVE}\r\n{GEN_LINE}\r\n\r\n  \n", LIVE),
        (f"{GEN_LINE}\n{LIVE}\n", LIVE),
        (f"## Why\n\n{LIVE}\n\nw\n\n{GEN_LINE}\n", None),
        (f"## Why\n\nw\n\n{GEN_LINE}\n", None),
        (f"## Why\n\nw\n\n {LIVE}\n", None),
        ("", None),
        (None, None),
    ],
)
def test_edit_copies_the_live_opened_by_line(
    world: World, agent: dict[str, str], body: object, kept: str | None
) -> None:
    world.fake("gh", cases={"pr view": live_body(body)})
    write_draft(world)
    assert run_main("edit", str(DRAFT), "--pr", "7", env=agent) == 0
    view, _, edit = world.calls("gh")
    assert view["argv"] == ["pr", "view", "7", "--json", "body,url"]
    assert cast("list[str]", edit["argv"])[:3] == ["pr", "edit", "7"]
    opened_by = f"{kept}\n" if kept else ""
    assert str(edit["stdin"]).endswith(f"\nv\n\n{opened_by}Assisted-by: {POSTING}\n")
    assert not ledgers(world).exists()


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (live_body("", rc=1, stderr="no pull\nrequest"), "no pull request"),
        ({"rc": 0, "stdout": "not json", "stderr": ""}, "not json"),
        ({"rc": 0, "stdout": "[]", "stderr": ""}, "[]"),
    ],
)
def test_edit_stops_when_it_cant_read_the_live_body(
    world: World, answer: dict[str, object], why: str
) -> None:
    world.fake("gh", cases={"pr view": answer})
    write_draft(world)
    message = f"cannot read the body of PR 7 to keep its Opened-by: {why}"
    with pytest.raises(SystemExit, match=f"^{re.escape(message)}$"):
        run_main("edit", str(DRAFT), "--pr", "7")
    argvs = [cast("list[str]", call["argv"]) for call in world.calls("gh")]
    assert [argv[:2] for argv in argvs] == [["pr", "view"]]


def test_edit_stops_when_gh_fails_with_a_body(world: World) -> None:
    answer: dict[str, object] = {"rc": 1, "stdout": '{"body": ""}', "stderr": ""}
    world.fake("gh", cases={"pr view": answer})
    write_draft(world)
    message = 'cannot read the body of PR 7 to keep its Opened-by: {"body": ""}'
    with pytest.raises(SystemExit, match=f"^{re.escape(message)}$"):
        run_main("edit", str(DRAFT), "--pr", "7")


def test_every_invalid_trailer_is_joined_by_semicolons(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit("one\n\nGenerated-by: LLM model=a:m\nAssisted-by: LLM model=b:m")
    with pytest.raises(SystemExit) as info:
        pr_attribution.description(
            checked_for(), {}, pr_attribution.branch_trailers("main")
        )
    assert str(info.value) == (
        "invalid LLM trailer: LLM model=a:m: harness required; "
        "LLM model=b:m: harness required"
    )
