"""Find existing reviews and check whether they are still live."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.driver.decisions.reviews import (
    check_live_review,
    past_time_limit,
    review_key,
)
from tbhbdev.alpha.agent.driver.metadata import directory, load
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver.decisions.metadata import Run
    from tbhbdev.alpha.agent.model import Settings


def refuse_live_duplicate(settings: Settings, run: Run) -> None:
    """Refuse a duplicate review or a fifth live review of the same commit.

    Two runs on one repository, commit and profile coexist only when both
    declare distinct variants. A run with no variant conflicts with every
    live run on that key. The cap applies across variants.

    A recorded run counts as live until the host reports its exit status, so
    an unreachable host or a run with no status also refuses. A run older than
    its time limit no longer counts, because the host stops it by then. A
    record with no time limit stays live for six hours. The caller holds
    ``launch_lock`` so two launches cannot both pass this check.

    Raises:
        Invalid: A live review conflicts on variant or the cap is reached.
    """
    key = review_key(run)
    live = 0
    for path in sorted(directory().glob("*.json")):
        try:
            other = load(settings, path.stem)
        except Invalid, ValueError, FileNotFoundError:
            continue
        if review_key(other) != key or past_time_limit(
            settings, other, datetime.now(UTC)
        ):
            continue
        try:
            ended = (
                transport.read(settings, other.host, (other.name, other.owner)).rc
                is not None
            )
        except transport.TransportError:
            ended = False
        if ended:
            continue
        live += 1
        check_live_review(run, other, live)
