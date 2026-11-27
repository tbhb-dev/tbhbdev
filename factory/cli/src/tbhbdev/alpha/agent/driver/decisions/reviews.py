"""Apply duplicate review limits to recorded runs."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Profile, Settings


MAX_LIVE_REVIEWS = 4


# A recorded run with no known time limit counts as live this long.
UNKNOWN_LIMIT_SECONDS = 6 * 60 * 60


def past_time_limit(settings: Settings, run: Run, now: datetime) -> bool:
    """Whether RUN, recorded at launch, can no longer be running.

    The limit is the shortest of the request's timeout and the host run cap, or
    six hours when neither is known. The launch wait and the kill grace add to
    it, because the clock starts at preparation and the run starts later.
    """
    limits = [run.request.timeout]
    if settings.max_run_minutes is not None:
        limits.append(settings.max_run_minutes * 60)
    seconds = min((n for n in limits if n is not None), default=UNKNOWN_LIMIT_SECONDS)
    seconds += settings.launch_wait_seconds + settings.kill_grace_seconds
    age = now - datetime.fromisoformat(run.started)
    return age.total_seconds() > seconds


def canonical_repo(repo: str) -> str:
    """Lowercase owner/name, without a github.com prefix or a .git suffix."""
    name = repo.strip().lower()
    for prefix in ("https://github.com/", "github.com/"):
        name = name.removeprefix(prefix)
    return name.removesuffix(".git")


def review_key(run: Run) -> tuple[str, str | None, Profile]:
    return canonical_repo(run.request.repo), run.request.commit, run.request.profile


def check_live_review(run: Run, other: Run, live: int) -> None:
    if live >= MAX_LIVE_REVIEWS:
        message = (
            f"{run.name}: {run.request.repo}@{run.request.commit} already has "
            f"{MAX_LIVE_REVIEWS} live reviews"
        )
        raise Invalid(message)
    variant, other_variant = run.request.variant, other.request.variant
    if None not in (variant, other_variant) and variant != other_variant:
        return
    message = (
        f"{run.name}: run {other.name} already reviews "
        f"{run.request.repo}@{run.request.commit} with the "
        f"{run.request.profile.value} profile and hasn't ended. "
        f"Check `tbhbdev alpha agent status {other.name}` and use "
        f"`tbhbdev alpha agent clean {other.name}` once it ends. "
        "For concurrent reviewers, launch both with distinct --variant values"
    )
    raise Invalid(message)
