"""Parse resource readings and decide whether to admit a run."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import LINUX

from tbhbdev.alpha.agent.decisions import limits
from tbhbdev.alpha.agent.model import Os

FINE = limits.Reading(1.0, 8, 1000, 500, 100, 10, None)


def test_parse_reads_seven_fields() -> None:
    assert limits.parse("0.5 4 1000 900 100 0 -\n") == limits.Reading(
        0.5, 4, 1000, 900, 100, 0, None
    )
    assert limits.parse("0.5 4 1000 900 100 0 12.5\n") == limits.Reading(
        0.5, 4, 1000, 900, 100, 0, 12.5
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1 2 3",
        "1 4 1000 900 100 0",
        "1 4 1000 900 100 0 x",
        "1 4 1000 900 100 0 nan",
        "1 4 1000 900 100 0 -1",
        "x 4 1000 900 100 0 -",
        "1 4 1000 900 100 0 - 7",
        "nan 4 1000 900 100 0 -",
        "-1 4 1000 900 100 0 -",
        "1 0 1000 900 100 0 -",
        "1 4 0 0 100 0 -",
        "1 4 1000 -1 100 0 -",
        "1 4 1000 900 -1 0 -",
        "1 4 1000 900 100 -1 -",
    ],
)
def test_parse_refuses_anything_else(text: str) -> None:
    assert limits.parse(text) == "unparsable output"


def test_decide_passes_a_quiet_host() -> None:
    assert limits.decide(LINUX, FINE, 0, ignore=False) == (False, "")


def test_decide_names_every_number_over_a_limit() -> None:
    busy = limits.Reading(8.0, 8, 1000, 100, 100, 90, None)
    refuse, message = limits.decide(LINUX, busy, 0, ignore=False)
    assert refuse
    assert message == (
        "example-linux: load 8 >= limit 8; memory available 10% < 20%; "
        "swap used 90% > 80%; pass --ignore-limits to launch anyway"
    )


def test_decide_uses_the_configured_limits() -> None:
    host = dataclasses.replace(
        LINUX, load_limit=24, min_memory_available_percent=10, max_swap_used_percent=95
    )
    reading = limits.Reading(23.9, 32, 1000, 100, 100, 90, None)
    assert limits.decide(host, reading, 0, ignore=False) == (False, "")
    low = dataclasses.replace(host, load_limit=2)
    assert limits.decide(low, reading, 0, ignore=False)[0]


FULL_SWAP = limits.Reading(1.0, 8, 1000, 500, 100, 99, 0.5)
MAC = dataclasses.replace(LINUX, os=Os.MACOS)


def test_full_swap_with_low_pressure_launches_with_a_warning() -> None:
    assert limits.decide(LINUX, FULL_SWAP, 0, ignore=False) == (
        False,
        "warning: example-linux: swap used 99% > 80%",
    )


def test_high_pressure_refuses_and_still_names_the_swap() -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=10.0)
    expected = (
        "example-linux: memory pressure avg10 10 >= limit 10; "
        "swap used 99% > 80%; pass --ignore-limits to launch anyway"
    )
    assert limits.decide(LINUX, found, 0, ignore=False) == (True, expected)
    quiet = dataclasses.replace(FULL_SWAP, swap_used=0, pressure=9.9)
    assert limits.decide(LINUX, quiet, 0, ignore=False) == (False, "")
    assert limits.decide(
        LINUX, dataclasses.replace(quiet, pressure=12), 0, ignore=True
    )[1].startswith("warning: ")


def test_unknown_pressure_keeps_the_swap_refusal() -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=None)
    assert limits.decide(LINUX, found, 0, ignore=False) == (
        True,
        "example-linux: swap used 99% > 80%; pass --ignore-limits to launch anyway",
    )
    assert limits.decide(
        LINUX, dataclasses.replace(found, swap_used=0), 0, ignore=False
    ) == (
        False,
        "",
    )


@pytest.mark.parametrize(("level", "refuse"), [(1, False), (2, False), (4, True)])
def test_macos_levels(level: int, refuse: bool) -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=level)
    assert limits.decide(MAC, found, 0, ignore=False)[0] is refuse
    if refuse:
        assert (
            "memory pressure level 4 >= limit 4"
            in limits.decide(MAC, found, 0, ignore=False)[1]
        )


def test_the_pressure_limits_are_per_host() -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=2)
    host = dataclasses.replace(MAC, max_memory_pressure_level=2)
    assert limits.decide(host, found, 0, ignore=False)[0]
    busy = dataclasses.replace(LINUX, max_memory_pressure_avg10=1)
    assert limits.decide(busy, dataclasses.replace(found, pressure=1), 0, ignore=False)[
        0
    ]


def test_macos_level_one_refuses_when_the_host_limit_is_one() -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=1)
    host = dataclasses.replace(MAC, max_memory_pressure_level=1)
    refuse, message = limits.decide(host, found, 0, ignore=False)
    assert refuse
    assert "memory pressure level 1 >= limit 1" in message
    assert not limits.decide(MAC, found, 0, ignore=False)[0]


def test_decide_allows_swap_of_zero() -> None:
    reading = limits.Reading(1.0, 8, 1000, 500, 0, 0, None)
    assert limits.decide(LINUX, reading, 0, ignore=False) == (False, "")


def test_decide_refuses_a_failed_read() -> None:
    refuse, message = limits.decide(LINUX, "cause", 0, ignore=False)
    assert refuse
    assert message == (
        "example-linux: cannot read host limits: cause; "
        "pass --ignore-limits to launch anyway"
    )


@pytest.mark.parametrize(
    "found", ["cause", limits.Reading(9, 8, 1000, 500, 0, 0, None)]
)
def test_decide_warns_when_ignored_or_warn_only(found: limits.Reading | str) -> None:
    warn_only = dataclasses.replace(LINUX, limits_warn_only=True)
    for refuse, message in (
        limits.decide(LINUX, found, 0, ignore=True),
        limits.decide(warn_only, found, 0, ignore=False),
    ):
        assert not refuse
        assert message.startswith("warning: example-linux: ")
