"""Parse resource readings and decide whether to admit a run."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import LINUX

from tbhbdev.alpha.agent.decisions import limits
from tbhbdev.alpha.agent.model import Host, Os

FINE = limits.Reading(1.0, 8, 1000, 500, 100, 10, None)


@pytest.mark.parametrize(
    ("tail", "pressure", "swap_in"),
    [("- -", None, None), ("12.5 -", 12.5, None), ("- 7.5", None, 7.5)],
)
def test_parse_reads_eight_fields(
    tail: str, pressure: float | None, swap_in: float | None
) -> None:
    found = limits.parse(f"0.5 4 1000 900 100 0 {tail}\n")
    assert found == limits.Reading(0.5, 4, 1000, 900, 100, 0, pressure, swap_in)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1 2 3",
        "1 4 1000 900 100 0",
        "0.5 4 1000 900 100 0 -",
        "0.5 4 1000 900 100 0 12.5",
        "1 4 1000 900 100 0 - 7 extra",
        *(f"1 4 1000 900 100 0 {p} -" for p in ("x", "nan", "-1")),
        *(f"1 4 1000 900 100 0 - {s}" for s in ("-1", "x", "nan", "inf")),
        *(f"{x} 4 1000 900 100 0 - -" for x in ("x", "nan", "-1")),
        "1 0 1000 900 100 0 - -",
        "1 4 0 0 100 0 - -",
        "1 4 1000 -1 100 0 - -",
        "1 4 1000 900 -1 0 - -",
        "1 4 1000 900 100 -1 - -",
    ],
)
def test_parse_refuses_anything_else(text: str) -> None:
    assert limits.parse(text) == "unparsable output"


def test_decide_passes_a_quiet_host() -> None:
    assert limits.decide(LINUX, FINE, 0, recent=0, ignore=False) == (False, "")


def test_decide_names_every_number_over_a_limit() -> None:
    busy = limits.Reading(8.0, 8, 1000, 100, 100, 90, None)
    refuse, message = limits.decide(LINUX, busy, 0, recent=0, ignore=False)
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
    assert limits.decide(host, reading, 0, recent=0, ignore=False) == (False, "")
    low = dataclasses.replace(host, load_limit=2)
    assert limits.decide(low, reading, 0, recent=0, ignore=False)[0]


FULL_SWAP = limits.Reading(1.0, 8, 1000, 500, 100, 99, 0.5)
MAC = dataclasses.replace(LINUX, os=Os.MACOS)


def test_full_swap_with_low_pressure_launches_with_a_warning() -> None:
    expected = (False, "warning: example-linux: swap used 99% > 80%")
    assert limits.decide(LINUX, FULL_SWAP, 0, recent=0, ignore=False) == expected
    found = dataclasses.replace(FULL_SWAP, swap_in=5000)
    assert limits.decide(LINUX, found, 0, recent=0, ignore=False) == expected


def test_high_pressure_refuses_and_still_names_the_swap() -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=10.0)
    expected = (
        "example-linux: memory pressure avg10 10 >= limit 10; "
        "swap used 99% > 80%; pass --ignore-limits to launch anyway"
    )
    assert limits.decide(LINUX, found, 0, recent=0, ignore=False) == (True, expected)
    quiet = dataclasses.replace(FULL_SWAP, swap_used=0, pressure=9.9)
    assert limits.decide(LINUX, quiet, 0, recent=0, ignore=False) == (False, "")
    assert limits.decide(
        LINUX, dataclasses.replace(quiet, pressure=12), 0, recent=0, ignore=True
    )[1].startswith("warning: ")


@pytest.mark.parametrize("host", [LINUX, MAC], ids=["linux", "macos"])
def test_unknown_pressure_and_swap_in_keep_the_swap_refusal(host: Host) -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=None)
    assert limits.decide(host, found, 0, recent=0, ignore=False) == (
        True,
        f"{host.name}: swap used 99% > 80%; pass --ignore-limits to launch anyway",
    )
    assert limits.decide(
        host, dataclasses.replace(found, swap_used=0), 0, recent=0, ignore=False
    ) == (
        False,
        "",
    )


@pytest.mark.parametrize("delta", [-1000.0, -1.0, 0.0, 4000.0])
def test_swap_in_is_used_when_pressure_is_unknown(delta: float) -> None:
    limit = limits.MAX_SWAP_IN_PAGES_PER_SECOND
    assert limit == 1000
    rate, refuse = limit + delta, delta >= 0
    reading = dataclasses.replace(FULL_SWAP, pressure=None, swap_in=rate)
    swap = "swap used 99% > 80%"
    msg = (
        f"{LINUX.name}: swap-in {rate:g} pages/s >= limit {limit}; {swap}; "
        "pass --ignore-limits to launch anyway"
        if refuse
        else f"warning: {LINUX.name}: {swap}"
    )
    assert limits.decide(LINUX, reading, 0, recent=0, ignore=False) == (refuse, msg)


@pytest.mark.parametrize(("level", "refuse"), [(1, False), (2, False), (4, True)])
def test_macos_levels(level: int, refuse: bool) -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=level)
    assert limits.decide(MAC, found, 0, recent=0, ignore=False)[0] is refuse
    if refuse:
        assert (
            "memory pressure level 4 >= limit 4"
            in limits.decide(MAC, found, 0, recent=0, ignore=False)[1]
        )


def test_the_pressure_limits_are_per_host() -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=2)
    host = dataclasses.replace(MAC, max_memory_pressure_level=2)
    assert limits.decide(host, found, 0, recent=0, ignore=False)[0]
    busy = dataclasses.replace(LINUX, max_memory_pressure_avg10=1)
    assert limits.decide(
        busy, dataclasses.replace(found, pressure=1), 0, recent=0, ignore=False
    )[0]


def test_macos_level_one_refuses_when_the_host_limit_is_one() -> None:
    found = dataclasses.replace(FULL_SWAP, pressure=1)
    host = dataclasses.replace(MAC, max_memory_pressure_level=1)
    refuse, message = limits.decide(host, found, 0, recent=0, ignore=False)
    assert refuse
    assert "memory pressure level 1 >= limit 1" in message
    assert not limits.decide(MAC, found, 0, recent=0, ignore=False)[0]


def test_decide_allows_swap_of_zero() -> None:
    reading = limits.Reading(1.0, 8, 1000, 500, 0, 0, None)
    assert limits.decide(LINUX, reading, 0, recent=0, ignore=False) == (False, "")


def test_decide_refuses_a_failed_read() -> None:
    refuse, message = limits.decide(LINUX, "cause", 0, recent=0, ignore=False)
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
        limits.decide(LINUX, found, 0, recent=0, ignore=True),
        limits.decide(warn_only, found, 0, recent=0, ignore=False),
    ):
        assert not refuse
        assert message.startswith("warning: example-linux: ")


def test_parse_probe_reads_total_and_recent_counts() -> None:
    expected = limits.Reading(0.5, 4, 1000, 900, 100, 0, None)
    row = "0.5 4 1000 900 100 0 - -"
    assert limits.parse_probe(f"{row}\n") == (expected, 0, 0)
    assert limits.parse_probe(f"{row}\n2\n") == (expected, 2, 2)
    assert limits.parse_probe(f"{row}\n5 2\n") == (expected, 5, 2)
    assert limits.parse_probe(f"{row}\n0 0\n") == (expected, 0, 0)
    assert limits.parse_probe("") == ("unparsable output", 0, 0)
    bad = ("bad", "2 bad", "2 3", "2 1 1", "-1 0", "2\textra", "2\nextra")
    for tail in bad:
        assert limits.parse_probe(f"{row}\n{tail}\n") == ("unparsable output", 0, 0)


def test_decide_charges_only_recent_runs_in_the_memory_reservation() -> None:
    mib, kib = 6144, 6144 * 1024
    roomy = dataclasses.replace(LINUX, min_memory_available_percent=0)

    def check(available: int, recent: int) -> tuple[bool, str]:
        reading = limits.Reading(1.0, 8, 64 * 1024 * 1024, available, 0, 0, None)
        return limits.decide(roomy, reading, mib, recent=recent, ignore=False)

    refuse, message = check(kib * 3 // 2, 3)
    assert refuse
    assert f"memory available {kib * 3 // 2} KiB" in message
    assert f"< 4 * reservation {mib} MiB (3 recently admitted)" in message
    assert check(kib, 0) == (False, "")
    assert check(4 * kib, 3) == (False, "")
    assert check(4 * kib - 1, 3)[0]
    one_run = check(kib, 1)
    assert one_run[0]
    assert f"< 2 * reservation {mib} MiB (1 recently admitted)" in one_run[1]
    assert check(2 * kib, 1) == (False, "")
    short = check(kib - 1, 0)
    assert short[0]
    assert f"< reservation {mib} MiB;" in short[1]
    assert "recently admitted" not in short[1]
