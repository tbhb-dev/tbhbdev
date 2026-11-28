"""Launch limits over host numbers; no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Os

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host

_FIELDS = 8
_UNKNOWN = "-"
MAX_SWAP_IN_PAGES_PER_SECOND = 1000


@dataclass(frozen=True)
class Reading:
    """The host's 1-minute load and CPU count, with memory and swap in KiB.

    PRESSURE is Linux's memory `some avg10` or macOS's pressure level.
    SWAP_IN is Linux's swap-in rate in pages per second. An unknown reading
    for either is None.
    """

    load: float
    cpus: int
    memory_total: int
    memory_available: int
    swap_total: int
    swap_used: int
    pressure: float | None
    swap_in: float | None = None


def parse(text: str) -> Reading | str:
    """The host reading in TEXT, or the cause of failure.

    The last two fields are memory pressure and swap-in, with `-` for unknown.
    """
    parts = text.split()
    try:
        if len(parts) != _FIELDS:
            raise ValueError  # noqa: TRY301 -- one cause for every bad shape
        pressure, swap_in = (None if p == _UNKNOWN else float(p) for p in parts[6:])
        load = float(parts[0])
        cpus, total, available, swap_total, swap_used = (int(p) for p in parts[1:6])
        found = Reading(
            load, cpus, total, available, swap_total, swap_used, pressure, swap_in
        )
    except ValueError:
        return "unparsable output"
    sizes = (found.memory_available, found.swap_total, found.swap_used)
    if not (
        math.isfinite(found.load)
        and found.load >= 0
        and found.cpus > 0
        and found.memory_total > 0
        and min(sizes) >= 0
        and all(
            value is None or (math.isfinite(value) and value >= 0)
            for value in (found.pressure, found.swap_in)
        )
    ):
        return "unparsable output"
    return found


def parse_probe(text: str) -> tuple[Reading | str, int, int]:
    """Parse line 1 as a host reading and optional line 2 as `TOTAL [RECENT]`."""
    match text.splitlines():
        case [reading]:
            counts: list[str] = []
        case [reading, count_line]:
            counts = count_line.split()
        case _:
            return "unparsable output", 0, 0
    if not all(c.isascii() and c.isdigit() for c in counts):
        return "unparsable output", 0, 0
    found = parse(reading)
    match [int(c) for c in counts]:
        case []:
            return found, 0, 0
        case [total]:
            return found, total, total
        case [total, recent] if recent <= total:
            return found, total, recent
        case _:
            return "unparsable output", 0, 0


def _pressure(host: Host, found: Reading) -> tuple[list[str], list[str]]:
    """The refusals and the warnings that FOUND's pressure and swap raise.

    Swap fill only warns when either pressure or swap-in is known. Linux can
    keep cold pages in swap long after pressure ends.
    """
    swap = ""
    if 100 * found.swap_used > host.max_swap_used_percent * found.swap_total:
        used = 100 * found.swap_used / found.swap_total
        swap = f"swap used {used:.0f}% > {host.max_swap_used_percent}%"
    warnings = [swap] if swap else []
    value = found.pressure
    unit = ""
    if value is None:
        value = found.swap_in
        if value is None:
            return warnings, []
        limit, name = MAX_SWAP_IN_PAGES_PER_SECOND, "swap-in"
        unit = " pages/s"
    elif host.os is Os.MACOS:
        limit, name = host.max_memory_pressure_level, "memory pressure level"
    else:
        limit, name = host.max_memory_pressure_avg10, "memory pressure avg10"
    refusals = [f"{name} {value:g}{unit} >= limit {limit}"] if value >= limit else []
    return refusals, warnings


def _reservation(available: int, memory_mib: int, recent: int) -> str | None:
    if available >= (recent + 1) * memory_mib * 1024:
        return None
    target = (
        f"{recent + 1} * reservation {memory_mib} MiB ({recent} recently admitted)"
        if recent
        else f"reservation {memory_mib} MiB"
    )
    return f"memory available {available} KiB < {target}"


def _problems(
    host: Host, found: Reading, memory_mib: int, recent: int
) -> tuple[list[str], list[str]]:
    limit = found.cpus if host.load_limit is None else host.load_limit
    memory = 100 * found.memory_available / found.memory_total
    problems: list[str] = []
    if found.load >= limit:
        problems.append(f"load {found.load:g} >= limit {limit}")
    if memory < host.min_memory_available_percent:
        problems.append(
            f"memory available {memory:.0f}% < {host.min_memory_available_percent}%"
        )
    if reservation := _reservation(found.memory_available, memory_mib, recent):
        problems.append(reservation)
    refusals, notes = _pressure(host, found)
    problems.extend(refusals)
    return problems, notes


def decide(
    host: Host,
    found: Reading | str,
    memory_mib: int,
    *,
    recent: int,
    ignore: bool,
) -> tuple[bool, str]:
    """Whether to refuse the launch, and the message to print, empty if none.

    FOUND is the reading, or the cause of a failed read. RECENT is the count of
    admitted runs claimed within the admission window, which the host's
    available memory can't yet reflect; older runs are already subtracted from
    it. A warn-only host and IGNORE turn a refusal into a warning.
    """
    notes: list[str] = []
    if isinstance(found, str):
        problems = [f"cannot read host limits: {found}"]
    else:
        problems, notes = _problems(host, found, memory_mib, recent)
    if not problems:
        return False, f"warning: {host.name}: " + "; ".join(notes) if notes else ""
    message = f"{host.name}: " + "; ".join(problems + notes)
    if host.limits_warn_only or ignore:
        return False, f"warning: {message}"
    return True, f"{message}; pass --ignore-limits to launch anyway"
