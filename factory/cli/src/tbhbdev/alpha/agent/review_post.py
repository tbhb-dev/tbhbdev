"""Post a review run's verdict host-side with the run's token.

The sandbox writes the draft file. Run-host validates it, adds the run's
identity to the body and posts it against the pinned commit after the run ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx2

from tbhbdev import github_tls
from tbhbdev.alpha import gh_app_api, review, review_doc
from tbhbdev.alpha.agent import credentials, runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions import request as requests
from tbhbdev.alpha.agent.model import Harness, Invalid
from tbhbdev.alpha.identity import models as identity_models
from tbhbdev.alpha.identity import records as identity_records
from tbhbdev.alpha.identity import trailers as identity_trailers
from tbhbdev.alpha.identity.commit import grammar as identity_commit_grammar
from tbhbdev.alpha.pull_request.frontmatter import parse_frontmatter
from tbhbdev.alpha.size_gate import SIZE_REVIEW, size_verdict
from tbhbdev.content_review import check_review

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host.state import Prepared, Run

VERDICTS = frozenset({"GO", "BLOCK", "NO-GO"})
DRAFT_NAME = re.compile(r"review-([1-9][0-9]*)(?:\.doc)?\.md")
DRAFT_LIMIT = 64 * 1024
TIMEOUT = httpx2.Timeout(20.0, connect=5.0)


class PostRefusedError(review_doc.ReviewError):
    """The draft or the API refused; the message is safe to log."""


@dataclass(frozen=True)
class Submission:
    """One review to post: the draft text and what the post must match."""

    repository: str
    number: int
    text: str
    commit: str
    stamp: str
    document: bool = False


def find_draft(workspace: Path) -> tuple[int, str, bool]:
    """Read the run's draft using the same preference as the review CLI.

    Raises:
        PostRefusedError: The run did not leave drafts for exactly one pull request.
    """
    directory = workspace / ".git" / "tbhb-alpha"
    found = {
        int(match[1])
        for path in directory.glob("review-*.md")
        if (match := DRAFT_NAME.fullmatch(path.name))
    }
    if len(found) != 1:
        message = f"expected one review draft, found {len(found)}"
        raise PostRefusedError(message)
    (number,) = found
    path, document = review.select_draft(number, directory=directory)
    return number, runfiles.read_owned(path, DRAFT_LIMIT, "review draft"), document


def validate_document(sub: Submission) -> tuple[dict[str, object], str]:
    """Check a schema 2 draft against the pull request and pinned head."""
    try:
        doc = review.parse_document(sub.text, sub.number)
    except review_doc.ReviewError as exc:
        raise PostRefusedError(str(exc)) from exc
    _, owner, name = repo.parts(sub.repository)
    if doc.repo != f"{owner}/{name}":
        message = "the draft's repo isn't the run's repository"
        raise PostRefusedError(message)
    if doc.head_sha != sub.commit:
        message = "the draft's head_sha isn't the commit the run reviewed"
        raise PostRefusedError(message)
    if problems := check_review(doc.body, metadata=doc.metadata):
        raise PostRefusedError(problems[0])
    event = "APPROVE" if doc.verdict == "GO" else "REQUEST_CHANGES"
    body = f"{review_doc.render(doc)}\n{sub.stamp}\n"
    return {**doc.metadata, "event": event}, body


def validate(sub: Submission) -> tuple[dict[str, object], str]:
    """Check the draft host-side and return (frontmatter, body).

    Raises:
        PostRefusedError: The draft fails a check, names another head than the
            run reviewed, or asks for an event a run can't post.
    """
    if sub.document:
        return validate_document(sub)
    try:
        problems = review.check_text(sub.text, sub.number, lint=False)
    except RecursionError as exc:
        message = "the draft frontmatter is nested too deeply"
        raise PostRefusedError(message) from exc
    if problems:
        line, why = problems[0]
        message = f"the draft fails its checks: line {line}: {why}"
        raise PostRefusedError(message)
    meta, body = parse_frontmatter(sub.text)
    fields = cast("dict[str, object]", meta)
    if str(fields["commit_id"]).lower() != sub.commit.lower():
        message = "the draft's commit_id isn't the commit the run reviewed"
        raise PostRefusedError(message)
    prose = body.strip("\n")
    return fields, f"{prose}\n\n{sub.stamp}\n"


def api(
    method: str, url: str, token: str, payload: dict[str, object] | None = None
) -> object:
    """One GitHub API call; any 2xx is success.

    Raises:
        PostRefusedError: GitHub refuses or can't be reached.
    """
    try:
        response = httpx2.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json=payload,
            timeout=TIMEOUT,
            verify=github_tls.public_roots(),
        )
    except httpx2.HTTPError as exc:
        message = f"the {method} request failed: {type(exc).__name__}"
        raise PostRefusedError(message) from exc
    if not response.is_success:
        message = f"GitHub refused the {method} with status {response.status_code}"
        raise PostRefusedError(message)
    try:
        return response.json() if response.content else None
    except ValueError:
        # A 2xx whose body isn't JSON still means the request succeeded.
        return None


def pull_head(data: object) -> str | None:
    head = data.get("head") if isinstance(data, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) else None


def compare(run: Run, path: str) -> object:
    """Read the diff with the review run's read-only App permissions."""
    try:
        token = credentials.fetch_token(run.request, run.settings)
    except Invalid:
        token = None
    if token is None:
        message = "read-only reviews App token unavailable"
        raise PostRefusedError(message)
    return api("GET", path, token)


def create(sub: Submission, token: str, run: Run) -> int | None:
    """Validate, then post once; return the review id, or None if GitHub sent none.

    Raises:
        PostRefusedError: A check fails, an approval's head moved, or GitHub refuses.
    """
    fields, body = validate(sub)
    _, owner, name = repo.parts(sub.repository)
    base = f"https://api.github.com/repos/{owner}/{name}/pulls/{sub.number}"
    event = str(fields["event"])
    if event == "APPROVE" and pull_head(api("GET", base, token)) != sub.commit:
        message = "the pull request head moved since the run judged it"
        raise PostRefusedError(message)
    return review_doc.submit_review(
        lambda method, path, payload: api(method, path, token, payload),
        review_doc.ReviewSubmission(
            base, sub.commit, body, event, fields if sub.document else None
        ),
        compare=lambda path: compare(run, path),
        warn=lambda message: run.files.write("log", f"{message}\n"),
    )


def stamp(run: Run) -> str:
    """Build the host-written attribution for the review."""
    request = run.request
    key = (
        identity_models.ANTIGRAVITY
        if request.harness is Harness.AGY
        else request.harness.value
    )
    try:
        raw_effort = requests.review_effort(request)
    except Invalid as exc:
        raise PostRefusedError(str(exc)) from exc
    model, effort = identity_models.model_effort(key, request.model, raw_effort)
    value = identity_trailers.format_value(
        {
            "effort": effort or "",
            "harness": identity_models.SLUGS[key],
            "launch": identity_records.sha256_64(run.files.name),
            "model": model or "",
            "surface": request.kind.value,
            "variant": request.variant or "",
        }
    )
    if why := identity_commit_grammar.check_value(value):
        message = f"invalid LLM trailer: {why}"
        raise PostRefusedError(message)
    return f"{identity_trailers.KEY_GEN}: {value}"


def final_verdict_line(run: Run) -> str:
    """Read the last line of the run's final output."""
    for suffix in ("out.final", "out"):
        try:
            answer = runfiles.read_owned(
                run.files.path(suffix), 16 * 1024 * 1024, "review output"
            )
        except FileNotFoundError:
            continue
        return answer.rstrip().rpartition("\n")[2]
    return ""


def check_verdict(run: Run) -> None:
    """Require a final `VERDICT: GO|BLOCK|NO-GO` line, with the run's sha if any."""
    label, _, rest = final_verdict_line(run).partition(" ")
    verdict, _, sha = rest.partition(" ")
    if (
        label == "VERDICT:"
        and verdict in VERDICTS
        and (not sha or sha == run.request.commit)
    ):
        return
    message = "no verdict line at the end of the final answer, or it names another head"
    raise PostRefusedError(message)


def _posting_token(run: Run, permissions: dict[str, str]) -> str:
    try:
        token = credentials.review_post_token(
            run.request, run.settings, permissions=permissions
        )
    except Invalid as error:
        raise PostRefusedError(str(error)) from None
    if token is None:
        raise PostRefusedError(credentials.review_token_unavailable(permissions))
    return token


def post_size_review(run: Run) -> None:
    """Post a sizing decision as a status on the commit the run reviewed."""
    head = run.request.commit or ""
    verdict = size_verdict(final_verdict_line(run), head)
    if verdict is None:
        message = "no sizing verdict at the end of the final answer for the pinned head"
        raise PostRefusedError(message)
    token = _posting_token(run, credentials.SIZE_REVIEW_POST_PERMISSIONS)
    _, owner, name = repo.parts(run.request.repo)
    api(
        "POST",
        f"{gh_app_api.API}/repos/{owner}/{name}/statuses/{head}",
        token,
        {
            "context": SIZE_REVIEW,
            "state": "success" if verdict == "OVERRIDE" else "failure",
            "description": f"Sizing review verdict: {verdict}",
        },
    )
    run.files.write("review", f"{owner}/{name}@{head} {verdict}")


def posted(run: Run, prepared: Prepared) -> bool:
    """Ensure the run's review is posted and recorded."""
    if run.files.path("review").exists():
        return True
    workspace = Path(prepared.paths.worktree)
    try:
        if run.request.variant == SIZE_REVIEW:
            post_size_review(run)
            return True
        # A supplied output schema defines the final answer's format.
        if not run.files.path("schema.json").is_file():
            check_verdict(run)
        number, text, document = find_draft(workspace)
        token = _posting_token(run, credentials.REVIEW_POST_PERMISSIONS)
        sub = Submission(
            run.request.repo,
            number,
            text,
            run.request.commit or "",
            stamp(run),
            document,
        )
        identifier = create(sub, token, run)
    except review_doc.ReviewError as refusal:
        run.files.write("log", f"review not posted: {refusal}\n")
        return False
    run.files.write("review", f"{run.request.repo}#{number} {identifier or 'unknown'}")
    return True
