"""Check steering requests without contacting a run host."""

import pytest

from tbhbdev.alpha.agent.driver.decisions.steering import check_send
from tbhbdev.alpha.agent.model import Harness, Invalid


@pytest.mark.parametrize(
    ("harness", "text", "error"),
    [
        (Harness.CODEX, "hello", None),
        (Harness.CODEX, "op://example/item/field", "steering message refused"),
        (Harness.AGY, "hello", "send requires a Codex run"),
        (Harness.CLAUDE, "hello", "Claude delivery is not built"),
    ],
)
def test_check_send(harness: Harness, text: str, error: str | None) -> None:
    if error is None:
        check_send("sample", harness, text)
    else:
        with pytest.raises(Invalid, match=error):
            check_send("sample", harness, text)
