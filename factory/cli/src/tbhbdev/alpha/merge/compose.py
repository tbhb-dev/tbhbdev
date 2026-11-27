# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Read commit linter and Git results while building a merge message."""

from tbhbdev.alpha.identity.commit.grammar import LINE, check_message
from tbhbdev.alpha.merge.body import WIDTH, Syntax, render_body
from tbhbdev.alpha.merge.message import (
    Message,
    assemble,
    check_round_trip,
    check_subject,
    check_trailers,
    drop_opened,
    split_trailers,
)
from tbhbdev.alpha.pull_request import (
    FENCE,
    GIT,
    INLINE_CODE,
    OPENED_LINE,
    fence_flags,
    find_tool,
    heading_of,
    run,
    scan,
    split_blocks,
    title_problems,
    trailer_problems,
)

SYNTAX = Syntax(FENCE, INLINE_CODE, heading_of)


def build_message(
    title: str, number: int, body: str, *, agent: bool = False
) -> Message:
    """Check the message and confirm Git can read back the trailers."""
    subject = f"{title} (#{number})"
    check_subject(subject, title_problems(subject, WIDTH))
    raw = body.splitlines()
    lines, _ = scan(raw, 1)
    _, unclosed = fence_flags(raw)
    if unclosed is not None:
        error = (
            f"the PR body is malformed: line {unclosed + 1}: code fence is never closed"
        )
        raise SystemExit(error)
    rest, trailers = split_trailers(drop_opened(lines, LINE, OPENED_LINE), LINE)
    stray = [why for _, why in trailer_problems(rest)]
    check_trailers(trailers, stray, check_message("\n".join(trailers)), agent=agent)
    preamble, blocks, _ = split_blocks(rest)
    sections = [("", preamble), *((block.heading, block.lines) for block in blocks)]
    message = assemble(subject, render_body(sections, SYNTAX), trailers)
    if trailers:
        done = run(
            [find_tool(GIT), "interpret-trailers", "--parse"], stdin=message.text()
        )
        check_round_trip(done.stdout, trailers)
    return message
