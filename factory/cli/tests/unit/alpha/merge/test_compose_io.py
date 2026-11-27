# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests that build messages with the commit linter and Git."""

import json

import pytest
from unit.alpha.merge.examples import ASSIST, GEN, OPENED
from unit.alpha.merge.fake_github import Tools, read_back

from tbhbdev.alpha.identity.commit.grammar import check_message
from tbhbdev.alpha.merge.body import WIDTH
from tbhbdev.alpha.merge.compose import build_message
from tbhbdev.alpha.merge.github import fetch


def message_of(body: str, title: str = "fix: x", number: int = 7) -> str:
    return build_message(title, number, body).text()


@pytest.mark.usefixtures("tools")
def test_trailers_are_never_wrapped() -> None:
    long = GEN + ";surface=" + "x" * 90
    message = message_of(f"## Why\n\nw\n\n{long}\n")
    assert message.splitlines()[-1] == long


@pytest.mark.usefixtures("tools")
def test_trailers_alone_after_the_subject() -> None:
    assert message_of(f"{GEN}\n") == f"fix: x (#7)\n\n{GEN}\n"


@pytest.mark.usefixtures("tools")
def test_a_trailer_inside_a_paragraph_is_not_a_trailer_block() -> None:
    with pytest.raises(SystemExit, match="remove this line"):
        message_of(f"## Why\n\nw\n{GEN}\n\nmore\n")


@pytest.mark.usefixtures("tools")
def test_a_trailer_in_the_middle_is_refused() -> None:
    with pytest.raises(SystemExit, match="remove this line"):
        message_of(f"## Why\n\n{GEN}\n\nw\n")


@pytest.mark.usefixtures("tools")
def test_an_invalid_trailer_value_is_refused() -> None:
    with pytest.raises(SystemExit, match="invalid trailer"):
        message_of("## Why\n\nw\n\nGenerated-by: nonsense\n")


@pytest.mark.parametrize("key", ["Signed-off-by", "Co-authored-by", "signed-off-by"])
@pytest.mark.usefixtures("tools")
def test_human_trailers_in_the_body_are_refused(key: str) -> None:
    with pytest.raises(SystemExit, match="do not belong in the description"):
        message_of(f"## Why\n\nw\n\n{key}: A <a@b.c>\n")


@pytest.mark.usefixtures("tools")
def test_subject_gets_the_pr_number() -> None:
    assert build_message("feat(cli): add x", 12, "").subject == "feat(cli): add x (#12)"


@pytest.mark.usefixtures("tools")
def test_subject_over_72_with_the_suffix_fails() -> None:
    title = "feat: " + "a" * (72 - len("feat: ") - len(" (#5)") + 1)
    assert len(title) + len(" (#5)") == WIDTH + 1
    with pytest.raises(SystemExit, match="characters; the limit is 72"):
        build_message(title, 5, "")


@pytest.mark.usefixtures("tools")
def test_subject_of_exactly_72_passes() -> None:
    title = "feat: " + "a" * (72 - len("feat: ") - len(" (#5)"))
    assert len(build_message(title, 5, "").subject) == WIDTH


@pytest.mark.usefixtures("tools")
def test_subject_that_fails_cog_fails() -> None:
    with pytest.raises(SystemExit, match="cog verify"):
        build_message("BAD title", 5, "")


@pytest.mark.usefixtures("tools")
def test_an_unclosed_fence_is_refused() -> None:
    with pytest.raises(SystemExit, match="never closed"):
        message_of("## Why\n\n```\ncode\n")


def test_the_passing_draft_becomes_a_clean_message(tools: Tools) -> None:
    body = json.loads((tools.bin / "gh.spec").read_text())["view_out"]
    request = fetch(1)
    assert json.loads(body)["body"] == request.body
    message = build_message(request.title, 12, request.body).text()
    assert message == (
        "feat(cli): add the stamp subcommand (#12)\n\n"
        "Why\n"
        "Running `tool stamp` on a tree with a write-protected file stops\n"
        "partway. The command leaves some files stamped and others not, and\n"
        "prints nothing that says which. See the issue [1].\n\n"
        "Behavior change\n"
        "The command now checks every target before it writes one. A\n"
        "write-protected file fails the whole run with an exit code of 1 and a\n"
        "list of the blocked paths. A run that passes the check stamps every\n"
        "file.\n\n"
        "Verification\n"
        "Ran the new unit tests, then stamped a tree with one write-protected\n"
        "file:\n\n"
        "    $ go test ./internal/stamp/...\n"
        "    ok  \tgithub.com/tbhb-dev/tbhbdev/internal/stamp\t0.412s\n"
        "    $ tool stamp ./fixture\n"
        "    error: 1 file is read-only: fixture/a.txt\n\n"
        "No file in the tree changed after the failure.\n\n"
        "[1]: https://github.com/tbhb-dev/tbhbdev/issues/12\n"
    )


@pytest.mark.usefixtures("tools")
def test_the_fallback_identity_block_is_kept_and_passes_the_check() -> None:
    block = (
        "Generated-by: LLM effort=medium;harness=claude-code:2.1.316;"
        "model=anthropic:m;session=sha-256-64:71c761039ac2c7f9;surface=cli"
    )
    message = message_of(f"## Why\n\nw\n\n{block}\n")
    assert message.endswith(f"\n\n{block}\n")
    assert read_back(message) == f"{block}\n"
    assert check_message(message) == []


@pytest.mark.parametrize("tail", ["\n", "\n\n\n", "\n  \n"])
@pytest.mark.usefixtures("tools")
def test_the_opened_by_line_is_dropped_and_the_trailers_still_checked(
    tail: str,
) -> None:
    message = message_of(f"## Why\n\nw\n\n{OPENED}\n{GEN}\n{ASSIST}{tail}")
    assert message == f"fix: x (#7)\n\nWhy\nw\n\n{GEN}\n{ASSIST}\n"
    assert read_back(message) == f"{GEN}\n{ASSIST}\n"


@pytest.mark.usefixtures("tools")
def test_an_opened_by_line_anywhere_in_the_trailer_paragraph_is_dropped() -> None:
    message = message_of(f"## Why\n\nw\n\n{GEN}\n{OPENED}\n{ASSIST}\n")
    assert message == f"fix: x (#7)\n\nWhy\nw\n\n{GEN}\n{ASSIST}\n"


@pytest.mark.usefixtures("tools")
def test_an_opened_by_line_alone_leaves_no_trailer_block() -> None:
    assert message_of(f"## Why\n\nw\n\n{OPENED}\n") == "fix: x (#7)\n\nWhy\nw\n"
    assert message_of(f"{OPENED}\n{GEN}\n") == f"fix: x (#7)\n\n{GEN}\n"


@pytest.mark.usefixtures("tools")
def test_an_invalid_trailer_after_opened_by_is_still_refused() -> None:
    with pytest.raises(SystemExit, match="invalid trailer"):
        message_of(f"## Why\n\nw\n\n{OPENED}\nGenerated-by: nonsense\n")


@pytest.mark.parametrize(
    "body",
    [
        f"## Why\n\n{OPENED}\n\nw\n\n{GEN}\n",
        f"## Why\n\nw\n{OPENED}\n{GEN}\n",
        f"## Why\n\n```\n{OPENED}\n```\n\n{GEN}\n",
        f"## Why\n\n```\n{OPENED}\n```\n",
    ],
)
@pytest.mark.usefixtures("tools")
def test_an_opened_by_line_outside_the_trailer_paragraph_is_refused(
    body: str,
) -> None:
    with pytest.raises(SystemExit, match="the script writes Opened-by"):
        message_of(body)


@pytest.mark.usefixtures("tools")
def test_every_subject_problem_is_listed() -> None:
    title = "BAD " + "a" * 72
    with pytest.raises(SystemExit) as info:
        build_message(title, 5, "")
    assert str(info.value) == (
        f"the commit subject '{title} (#5)' fails: title is 81 characters; "
        "the limit is 72; title is not a Conventional Commit header (cog verify): "
        "not conventional"
    )


@pytest.mark.usefixtures("tools")
def test_an_unclosed_fence_names_its_line() -> None:
    with pytest.raises(SystemExit) as info:
        message_of("## Why\n\n```\ncode\n")
    assert str(info.value) == (
        "the PR body is malformed: line 3: code fence is never closed"
    )


@pytest.mark.usefixtures("tools")
def test_final_trailer_block_is_kept_verbatim_and_last() -> None:
    body = f"## Why\n\nw\n\n{GEN}\n{ASSIST}\n"
    message = message_of(body)
    assert message.endswith(f"\n\n{GEN}\n{ASSIST}\n")
    assert read_back(message) == f"{GEN}\n{ASSIST}\n"
