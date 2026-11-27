"""Validate Markdown pull request reviews against the CUE schema."""

import json
import subprocess  # nosec B404
from importlib.resources import as_file, files
from typing import cast

from tbhbdev import cue_runtime
from tbhbdev.content_model import normalize

_REVIEW_SECTION_FIXES = {
    "summary": "Write one Summary paragraph of one or two sentences "
    "and at most 40 words",
    "findings": "Use up to eight Findings bullets numbered F1 onward, with a "
    "severity, kind, and up to 30 words of evidence. "
    "Include an Occurs when: clause in each P1 finding",
    "suggestions": "Use up to five Suggestions bullets numbered S1 onward, "
    "with up to 25 words each",
    "verification": "Write Verification as one to six unnumbered bullets "
    "of at most 20 words each",
}


def _review_section_order_refusal(order: list[str]) -> str:
    if "summary" not in order:
        if "verdict" in order:
            return "Rename Verdict to Summary and give the reasons for the review"
        return "Add a Summary section"
    if "verification" not in order:
        return "Add a Verification section"
    return "Use Summary, optional Findings and Suggestions, then Verification"


def _review_refusal(stderr: str, draft: dict[str, object]) -> str:
    first = stderr.strip().partition("\n")[0]
    path, _, detail = first.partition(":")
    field, _, rest = path.partition(".")
    if field == "blocks":
        section, _, constraint = rest.partition(".")
        if constraint == "source" or (
            section == "summary" and constraint == "content.0.visible"
        ):
            return f"Remove the standalone verdict line from {section.title()}"
        return _REVIEW_SECTION_FIXES.get(section, "Review validation failed")
    if field == "_err":
        return f"Findings: {detail.strip().removesuffix(':')}"
    if field == "blockOrder":
        return _review_section_order_refusal(cast("list[str]", draft["blockOrder"]))
    if field == "metadata" or first.startswith("unresolved disjunction "):
        return "Correct the schema 2 review frontmatter"
    return "Review validation failed"


def check_review(
    markdown: str,
    *,
    metadata: dict[str, object] | None = None,
    event: str = "",
    posted: bool = False,
) -> list[str]:
    """Return review validation refusals, or an empty list on success."""
    definition = "#Review"
    if metadata is None:
        definition = "#ReviewBody"
        metadata = {"event": event} if event else {}
    try:
        draft = normalize(
            markdown,
            object="pr-review",
            variant="review",
            metadata={**metadata, "posted": posted},
        )
        payload = json.dumps(draft)
    except (TypeError, ValueError) as exc:
        return [str(exc)]
    try:
        cue = cue_runtime.resolve()
        with as_file(files().joinpath("schemas")) as schemas:
            # Fixed schema names and pinned executable. Draft JSON goes on stdin.
            done = subprocess.run(  # nosec B603
                [
                    str(cue),
                    "vet",
                    "-c",
                    "-d",
                    definition,
                    "core.cue",
                    "issue.cue",
                    "review.cue",
                    "json:",
                    "-",
                ],
                cwd=schemas,
                input=payload,
                capture_output=True,
                encoding="utf-8",
                check=False,
                timeout=10,
            )
    except (cue_runtime.CueError, OSError, subprocess.TimeoutExpired) as exc:
        return [f"Review validation could not run: {exc}"]
    if done.returncode:
        return [_review_refusal(done.stderr, draft)]
    return []
