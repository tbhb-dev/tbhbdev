import subprocess
from pathlib import Path

import pytest
from cue_fixtures import review_cue as review_cue  # noqa: PLC0414

from tbhbdev import cue_runtime
from tbhbdev.content_review import check_review

pytestmark = pytest.mark.usefixtures("review_cue")  # noqa: V107

SUMMARY = "## Summary\n\nThe cache preserves the last saved value.\n"
VERIFICATION = "\n## Verification\n\n- Read the cache implementation.\n"
FINDING = (
    "- F1: P1 correctness The cache returns stale values. "
    "Occurs when: a caller saves a record.\n"
)
DEBT = "- F1: P2 design Rename the fixture. It also serves the CLI.\n"
ATTRIBUTION = "\nGenerated-by: LLM harness=codex;model=m1\n"


def body_with(heading: str, text: str) -> str:
    before = "" if heading == "Summary" else SUMMARY + "\n"
    after = "" if heading == "Verification" else VERIFICATION
    return f"{before}## {heading}\n\n{text}\n{after}"


@pytest.mark.parametrize(
    ("heading", "item", "words", "count"),
    [
        ("Findings", "- F{}: P3 docs ", 30, 8),
        ("Suggestions", "- S{}: ", 25, 5),
        ("Verification", "- ", 20, 6),
    ],
)
def test_review_limits(heading: str, item: str, words: int, count: int) -> None:
    def body(size: int, items: int) -> str:
        prose = "word " * (size - 1) + "word."
        return body_with(
            heading, "\n".join(item.format(n) + prose for n in range(1, items + 1))
        )

    assert not check_review(body(words, count))
    assert check_review(body(words + 1, 1))
    assert check_review(body(1, count + 1))


def test_summary_counts_words_instead_of_characters() -> None:
    long_sentence = (
        "Independent verification establishes conformance between implementation "
        "and documented expectations by exercising representative interactions "
        "across configurations, workloads, operating environments, and deployment "
        "scenarios before gradual production rollout begins."
    )
    assert len(long_sentence.split()) == 26
    assert len(long_sentence) > 250
    assert not check_review(body_with("Summary", long_sentence))
    assert not check_review(body_with("Summary", "A " + "a " * 38 + "day."))


@pytest.mark.parametrize(
    "summary",
    [
        "A " + "a " * 39 + "day.",
        "The tests passed. The cache stays warm. Retries are safe.",
        "The driver validates the request. `pytest` passed. The host posts the review.",
        "The driver validates the request. retries passed. The host posts the review.",
        "The driver validates the request. 3 tests passed. The host posts the review.",
        "é" * 4097,
    ],
)
def test_summary_refuses_excess_words_sentences_or_size(summary: str) -> None:
    assert check_review(body_with("Summary", summary)) == [
        "Write one Summary paragraph of one or two sentences and at most 40 words"
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "Go-based fixtures cover the parser",
        "Go, Python, and Rust use the same fixture.",
        "Block the build when the request is invalid.",
        'The parser accepts "Retry?" as a label. Tests cover it.',
        "The parser accepts `Retry?` as a label. Tests cover it.",
        "The parser preserves `Retry? yes` as a single string. Tests cover it.",
        "The parser preserves “Retry? yes” as a single string. Tests cover it.",
        (
            "The parser preserves \u2018Retry? yes\u2019 as a single string. "
            "Tests cover it."
        ),
        (
            "The parser preserves \u2018Don\u2019t retry? yes\u2019 as a string. "
            "Tests cover it."
        ),
        "The handler returns the `APPROVE` enum.",
        "The handler checks each result, e.g. a stale value. Retries preserve it.",
        "The handler checks labels, e.g. Ready or Saved. Retries preserve them.",
        'The handler reports "Saved." The cache keeps the result.',
        'Retries are safe. The handler reports "Saved." The cache keeps the result.',
        "Dr. Lee inspected the cache. It keeps the result.",
        "Dr. Lee tested warm vs. cold caches. `pytest` passed.",
        "The handler recognizes `VERDICT: GO`.",
        "Decision: approve the change after the cache check.",
        "Verdict: Hold. The driver skips validation.",
        "The handler returns HOLD while the cache warms.",
        "The cache is warm. HOLD applies to stale requests.",
    ],
)
def test_summary_accepts_prose_without_interpreting_it(summary: str) -> None:
    assert not check_review(body_with("Summary", summary))


@pytest.mark.parametrize(
    "verdict",
    [
        "GO",
        "NO-GO",
        "APPROVE",
        "BLOCK",
        "HOLD",
        "REQUEST_CHANGES",
        "Decision: approve.",
        "VERDICT: NO-GO",
        "Verdict: Go.",
        "**APPROVE**",
    ],
)
@pytest.mark.parametrize(
    "surrounding", ["{}", "The tests ran.\n{}\nThe cache was warm."]
)
def test_summary_rejects_bare_and_prefixed_verdicts(
    verdict: str, surrounding: str
) -> None:
    assert check_review(body_with("Summary", surrounding.format(verdict))) == [
        "Remove the standalone verdict line from Summary"
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "GO. The driver validates the request.",
        "NO-GO: The cache returns a stale value.",
        "NO-GO because the cache returns a stale value.",
        "Decision: APPROVE. The driver validates the request.",
        "vErDiCt: HOLD until the cache is warm.",
    ],
)
def test_summary_rejects_leading_verdict_with_reasons(summary: str) -> None:
    assert check_review(body_with("Summary", summary)) == [
        "Remove the standalone verdict line from Summary"
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "The driver validates the request.\nRun: review-build",
        "**Provider:** codex",
        "The driver checked the judged head.",
        "The driver checked commit a123456.",
        f"The driver checked `{'a' * 40}`.",
    ],
)
def test_summary_rejects_review_stamps(summary: str) -> None:
    assert check_review(body_with("Summary", summary))


@pytest.mark.parametrize("verdict", ["GO", "BLOCK"])
def test_verification_refuses_standalone_verdict_line(verdict: str) -> None:
    assert check_review(
        (SUMMARY + VERIFICATION + f"\nVERDICT: {verdict}\n").replace("\n", "\r\n")
    ) == ["Remove the standalone verdict line from Verification"]


@pytest.mark.parametrize("evidence", ["src/app.py:4", "`src/app.py:4`"])
def test_review_accepts_path_line_evidence(evidence: str) -> None:
    body = body_with(
        "Findings",
        f"- F1: P1 correctness {evidence} returns stale values. "
        "Occurs when: a caller saves a record.",
    )
    assert not check_review(body, event="REQUEST_CHANGES")


@pytest.mark.parametrize("clause", ["", " Occurs when:", " Occurs when: ."])
def test_p1_requires_a_nonempty_occurs_when_clause(clause: str) -> None:
    body = body_with("Findings", f"- F1: P1 correctness The cache is stale.{clause}")
    assert "Occurs when:" in check_review(body, event="REQUEST_CHANGES")[0]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (VERIFICATION, "Add a Summary section"),
        (SUMMARY, "Add a Verification section"),
        (
            VERIFICATION + SUMMARY,
            "Use Summary, optional Findings and Suggestions, then Verification",
        ),
    ],
)
def test_review_reports_section_order(body: str, message: str) -> None:
    assert check_review(body) == [message]


@pytest.mark.parametrize("metadata", [{}, {"verdict": "GO", "repo": "invalid"}])
def test_review_reports_invalid_schema_two_metadata(
    metadata: dict[str, object],
) -> None:
    assert check_review(SUMMARY + VERIFICATION, metadata=metadata) == [
        "Correct the schema 2 review frontmatter"
    ]


@pytest.mark.parametrize(
    ("heading", "items"),
    [
        ("Findings", "- F1: P0 security Fix it.\n- F3: P2 test Write a test."),
        ("Findings", "- F1: P1 bug Fix it."),
        ("Findings", "- F1: P2 test **CI** is green."),
        ("Findings", "- F1: P2 test CI is green!"),
        ("Suggestions", "- S1: Add an example.\n- S3: Improve the label."),
        ("Suggestions", "- V1: Add an example."),
        ("Verification", "1. Read the cache."),
        ("Verification", "- V1: Read the cache."),
        ("Verification", "- [x] Read the cache."),
        ("Verification", "- Read the cache.\n\nA paragraph."),
        ("Verification", "- Read the cache.\nRun: review-build"),
        ("Verification", "- Read the cache.\n- Checked commit a123456."),
    ],
)
def test_review_rejects_invalid_items(heading: str, items: str) -> None:
    assert check_review(body_with(heading, items))


@pytest.mark.parametrize(
    ("event", "item", "refused"),
    [
        ("APPROVE", DEBT, False),
        ("APPROVE", FINDING, True),
        ("COMMENT", FINDING, True),
        ("REQUEST_CHANGES", FINDING, False),
        ("REQUEST_CHANGES", FINDING.replace("P1", "P0"), False),
        ("REQUEST_CHANGES", DEBT, True),
        ("REQUEST_CHANGES", "", True),
    ],
)
def test_review_event_matches_severity(event: str, item: str, *, refused: bool) -> None:
    findings = f"\n## Findings\n\n{item}" if item else ""
    problems = check_review(SUMMARY + findings + VERIFICATION, event=event)
    assert bool(problems) == refused


def test_posted_review_requires_a_mark_on_cited_findings() -> None:
    finding = "- F1: P2 design `cache.py:1` duplicates the default."
    body = body_with("Findings", finding) + ATTRIBUTION
    assert check_review(body, event="APPROVE", posted=True) == [
        (
            "Findings: Move an anchored finding to an inline comment, "
            "or mark it not anchored"
        )
    ]
    marked = finding + " (not anchored: line not in the diff)"
    assert not check_review(body_with("Findings", marked) + ATTRIBUTION, posted=True)


@pytest.mark.parametrize("priority", ["P0", "P1"])
def test_posted_review_counts_inline_blockers(priority: str) -> None:
    gap = DEBT.replace("F1:", "F3:")
    count = f"Inline findings: 1 ({priority} correctness 1)."
    body = body_with("Findings", f"{count}\n\n{gap}") + ATTRIBUTION
    assert not check_review(body, event="REQUEST_CHANGES", posted=True)
    assert check_review(body, event="APPROVE", posted=True) == [
        "Findings: P0 and P1 findings require REQUEST_CHANGES"
    ]
    assert check_review(body_with("Findings", f"{count}\n\n{gap}"))
    assert check_review(body_with("Findings", gap))


def test_check_review_reports_normalization_refusal() -> None:
    assert check_review("## <!-- invisible -->\n") == [
        "Section headings must have visible text"
    ]


def test_check_review_reports_missing_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Path:
        message = "run tbhbdev cue install"
        raise cue_runtime.CueError(message)

    monkeypatch.setattr(cue_runtime, "resolve", missing)
    assert check_review(SUMMARY + VERIFICATION) == [
        "Review validation could not run: run tbhbdev cue install"
    ]


def test_check_review_reports_silent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def quiet(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(["cue"], 1, stderr="\n")

    monkeypatch.setattr(cue_runtime, "resolve", lambda: Path("cue"))
    monkeypatch.setattr(subprocess, "run", quiet)
    assert check_review(SUMMARY + VERIFICATION) == ["Review validation failed"]
