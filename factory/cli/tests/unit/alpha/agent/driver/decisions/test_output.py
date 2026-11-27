"""Check how the coordinator formats launch and status results."""

from __future__ import annotations

from tbhbdev.alpha.agent.driver.decisions import output


def test_unsandboxed_reports_filter_authorization() -> None:
    reference = "o/r#1\x1b\x07\u200e"
    suffix = " sandbox=none authorized_by=o/r#1"
    assert (
        output.launched("example", "example-linux", "ops", authorized_by=reference)
        == "example: launched host=example-linux account=ops" + suffix
    )
    assert output.status(
        "example",
        phase="running",
        rc="running",
        target=("example-linux", "ops", reference, None),
        started="today",
    ) == (
        "example: phase=running rc=running host=example-linux account=ops started=today"
        " launcher=unknown" + suffix
    )


def test_launch() -> None:
    assert (
        output.launched("example", "example-linux", "agent")
        == "example: launched host=example-linux account=agent"
    )


def test_status() -> None:
    assert output.status(
        "example",
        phase="finishing",
        rc="0",
        target=("example-linux", "agent", None, "tools"),
        started="2026-11-09T00:00:00+00:00",
    ) == (
        "example: phase=finishing rc=0 host=example-linux "
        "account=agent started=2026-11-09T00:00:00+00:00 launcher=tools"
    )
