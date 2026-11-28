# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pure tests for merge message validation."""

import pytest
from unit.merge.examples import ASSIST, GEN, OPENED

from tbhbdev.alpha.identity.commit.grammar import LINE, check_message
from tbhbdev.alpha.pull_request.body import Line, scan, trailer_problems
from tbhbdev.alpha.pull_request.description import OPENED_LINE
from tbhbdev.merge.message import (
    Message,
    assemble,
    check_round_trip,
    check_subject,
    check_trailers,
    drop_opened,
    require_clean_message,
    split_trailers,
)


def test_a_trailer_git_cannot_read_back_fails() -> None:
    with pytest.raises(SystemExit, match="did not survive"):
        check_round_trip("other\n", [GEN])


def test_assembly_puts_trailers_after_the_rendered_sections() -> None:
    msg = assemble("fix: x (#7)", ["Why\nBecause."], [GEN, ASSIST])
    assert msg.text() == f"fix: x (#7)\n\nWhy\nBecause.\n\n{GEN}\n{ASSIST}\n"
    check_round_trip(f"{GEN}\n{ASSIST}\n", [GEN, ASSIST])


def test_opened_by_is_removed_from_the_final_trailer_block() -> None:
    lines, _ = scan(f"Why.\n\n{GEN}\n{OPENED}\n{ASSIST}\n".splitlines(), 1)
    rest, trailers = split_trailers(drop_opened(lines, LINE, OPENED_LINE), LINE)
    assert [line.text for line in rest] == ["Why.", ""]
    assert trailers == [GEN, ASSIST]


def test_subject_validation_uses_the_supplied_problems() -> None:
    with pytest.raises(SystemExit, match="too long; invalid type"):
        check_subject("bad", ["too long", "invalid type"])


@pytest.mark.parametrize(
    ("subject", "body"), [("fix: x (#7)", ""), ("Merge pull request #7", "body")]
)
def test_default_merge_message_is_refused(subject: str, body: str) -> None:
    with pytest.raises(SystemExit, match="GitHub's default message"):
        require_clean_message(Message(subject, body), 7)


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("trailers", [[GEN], [ASSIST], [GEN, ASSIST]])
def test_valid_final_trailers_are_accepted(agent: bool, trailers: list[str]) -> None:
    assert (
        check_trailers(trailers, [], check_message("\n".join(trailers)), agent=agent)
        is None
    )


def test_an_agent_pr_requires_a_final_trailer_paragraph() -> None:
    with pytest.raises(SystemExit) as info:
        check_trailers([], [], [], agent=True)
    assert str(info.value) == (
        "the PR body cannot be merged as is: "
        "agent PR is missing final trailer paragraph; "
        "add a valid trailer paragraph with tbhbdev alpha pr edit"
    )


@pytest.mark.parametrize("agent", [False, True])
def test_valid_final_trailers_do_not_excuse_a_stray_trailer(agent: bool) -> None:
    lines = [Line(1, GEN, GEN, code=False)]
    with pytest.raises(SystemExit) as info:
        check_trailers(
            [GEN], [why for _, why in trailer_problems(lines, LINE)], [], agent=agent
        )
    assert str(info.value) == (
        "the PR body cannot be merged as is: "
        "the script writes Generated-by and Assisted-by; remove this line; "
        "add a valid trailer paragraph with tbhbdev alpha pr edit"
    )


def test_trailer_problems_report_the_full_block_and_repair_command() -> None:
    check_trailers([], [], [], agent=False)
    block = ["Assisted-by: nope", GEN]
    with pytest.raises(SystemExit) as info:
        check_trailers(block, [], check_message("\n".join(block)), agent=False)
    assert str(info.value) == (
        "the PR body cannot be merged as is: invalid trailer block: "
        "Assisted-by: nope: must start with literal LLM; invalid trailer block: "
        f"{GEN}: Generated-by must come before Assisted-by; "
        "add a valid trailer paragraph with tbhbdev alpha pr edit"
    )
