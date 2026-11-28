import json
from pathlib import Path
from typing import cast

import pytest
from cue_fixtures import review_cue

from tbhbdev.content_review import check_review

__all__ = ["review_cue"]

EXAMPLES = (
    Path(__file__).resolve().parents[2] / "schemas" / "testdata" / "review-bodies"
)
_POSTED = ("Generated-by: LLM ", "<!-- tbhb-review v2 ")
_NAMES = sorted(path.stem for path in EXAMPLES.glob("*.txt"))


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize("name", _NAMES)
def test_review_body_example(name: str) -> None:
    assert _NAMES
    body = (EXAMPLES / f"{name}.txt").read_text(encoding="utf-8")
    raw = (EXAMPLES / f"{name}.json").read_text(encoding="utf-8")
    expected = cast("dict[str, object]", json.loads(raw))
    if expected.get("crlf"):
        body = body.replace("\n", "\r\n")
    if expected.get("superseded"):
        assert body.startswith("SUPERSEDED:")
        body = body.split("\n", 1)[1]
    posted = any(line.startswith(_POSTED) for line in body.splitlines())
    problems = check_review(body, posted=posted)
    assert bool(problems) != bool(expected["valid"]), f"{name}: {problems}"
