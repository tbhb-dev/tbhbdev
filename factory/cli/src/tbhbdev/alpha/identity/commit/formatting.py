# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Prepare identity trailers for a rewritten commit message."""

from __future__ import annotations

import re
from dataclasses import replace

from tbhbdev.alpha.identity.records import Json, hash_ok
from tbhbdev.alpha.identity.trailers import (
    KEY_ASSIST,
    KEY_GEN,
    Trailer,
    earliest_starts,
    format_value,
    llm_trailers,
    replace_trailer_fields,
    start_ok,
    trailer_agent,
)
lazy from tbhbdev.alpha.identity.models import Ident


def apply_record(
    value: str, root_rec: Json, rec: Json, branch_start: str | None
) -> str:
    """Replace the trailer's recorded launch and start attributes."""
    if not (rec or root_rec or branch_start):
        return value
    fields = {
        "launch": hash_ok(rec.get("launch")) or hash_ok(root_rec.get("launch")),
        "started_at": branch_start or start_ok(rec.get("started_at")),
    }
    return replace_trailer_fields(value, fields)


def identity_with_start(ident: Ident, root_rec: Json, rec: Json) -> Ident:
    """Fill launch and start attributes from the given records."""
    return replace(
        ident,
        launch=hash_ok(rec.get("launch")) or hash_ok(root_rec.get("launch")),
        started_at=start_ok(rec.get("started_at")),
    )


def trailer_candidates(
    text: str,
    gen: list[Ident],
    assisted: list[Ident],
    kept: list[Trailer],
) -> tuple[list[Trailer], list[Trailer]]:
    """Prepare old and new trailer candidates, letting new identities replace old."""
    new = [(KEY_GEN, format_value(i.attrs())) for i in gen] + [
        (KEY_ASSIST, format_value(i.attrs())) for i in assisted
    ]
    made = {(k, trailer_agent(v)) for k, v in new}
    older = [
        (k, v)
        for k, v in [*llm_trailers(text), *kept]
        if (k, trailer_agent(v)) not in made
    ]
    return older, new


SQUASH_MARK = " This is a combination of"


def should_merge_message(text: str, kept: list[Trailer], comment: str) -> bool:
    """Whether an amendment or squash needs its trailer blocks merged."""
    found = llm_trailers(text)
    # Git chooses auto from this candidate list after inspecting the message.
    markers = "#;@!$%^&|:" if comment == "auto" else (comment or "#")
    joined = bool(found) and re.search(
        rf"^[{re.escape(markers)}]{re.escape(SQUASH_MARK)}", text, re.MULTILINE
    )
    return bool(kept or joined or earliest_starts(found) != found)
