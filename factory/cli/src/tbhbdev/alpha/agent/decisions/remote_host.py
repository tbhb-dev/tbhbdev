"""Remote agent version, admission, limits, and repository probes."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions.remote_shell import GIT_SSH, PREFIX

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, Profile


def program(host: Host) -> str:
    return (
        shlex.quote(host.tbhbdev)
        if host.tbhbdev is not None
        else '"$HOME/.local/bin/tbhbdev"'
    )


def installed(host: Host) -> str:
    return PREFIX + f"{program(host)} --version"


def check_slot(host: Host, profile: Profile) -> str:
    command = shlex.join(
        (
            "alpha",
            "agent",
            "check-slot",
            host.name,
            profile,
            str(host.max_concurrent_runs),
            "--",
            *host.accounts,
        )
    )
    return "# tbhbdev-check-slot\n" + PREFIX + f"exec {program(host)} {command}\n"


PRESSURE_CHECK = "case \"$q\" in ''|*[!0-9.]*) q=-;; esac\n"


def limits(host: Host | None = None) -> str:
    """Print one line of host numbers: load, CPUs, then memory and swap in KiB.

    The fields are the 1-minute load, the logical CPU count, MemTotal,
    MemAvailable, swap total, swap used, memory pressure, and swap-in in pages
    per second. Pressure is Linux's `some avg10` or macOS's pressure level.
    Linux samples swap-in when pressure is unknown. Either field prints `-`
    when unknown. When HOST is given, prints `TOTAL RECENT`, the admitted runs
    and those admitted lately, on a second line if the host's tbhbdev supports
    it. An older one prints TOTAL.
    """
    linux = (
        "read l _ < /proc/loadavg || exit 1\n"
        "c=$(nproc) || exit 1\n"
        'n=$(awk -v l="$l" -v c="$c" \'/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} '
        "/^SwapTotal:/{s=$2} /^SwapFree:/{f=$2} "
        "END{if (t!~/^[0-9]+$/ || a!~/^[0-9]+$/ "
        "|| s!~/^[0-9]+$/ || f!~/^[0-9]+$/) exit 1; "
        "print l, c, t, a, s, s-f}' "
        "/proc/meminfo) || exit 1\n"
        r"""q=$(awk '$1=="some" && $2~/^avg10=[0-9]+(\.[0-9]+)?$/{print substr($2,7)}' \
    /proc/pressure/memory 2>/dev/null) || q=-
test -n "$q" || q=-
swap_in=-
if test "$q" = -; then
    pswpin() {
        awk '$1=="pswpin" && $2~/^[0-9]+$/{count=$2}
            END{if (count=="") exit 1; print count}' /proc/vmstat 2>/dev/null
    }
    if before=$(pswpin) && sleep 1 && after=$(pswpin) &&
        test "$after" -ge "$before"; then
        swap_in=$((after - before))
    fi
fi
printf "%s %s %s\n" "$n" "$q" "$swap_in"
"""
    )
    macos = (
        "l=$(sysctl -n vm.loadavg) || exit 1\n"
        "c=$(sysctl -n hw.logicalcpu) || exit 1\n"
        "m=$(sysctl -n hw.memsize) || exit 1\n"
        "u=$(sysctl -n vm.swapusage) || exit 1\n"
        "v=$(vm_stat) || exit 1\n"
        "q=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)\n"
        + PRESSURE_CHECK
        + 'printf "%s\\n" "$v" | awk -v l="$l" -v c="$c" -v m="$m" -v u="$u" '
        '-v q="$q" \''
        "/page size of/{z=$8} "
        "/^Pages (free|inactive|speculative|purgeable):/{p+=$NF; n++} "
        'END{split(l,x," "); split(u,w,"[ =M]+"); '
        'if (z=="" || n!=4 || x[2]=="" || w[1]!="total" || w[3]!="used" '
        "|| w[2]!~/^[0-9.]+$/ || w[4]!~/^[0-9.]+$/) exit 1; "
        'printf "%s %s %d %d %d %d %s -\\n", x[2], c, m/1024, p*z/1024, '
        "w[2]*1024, w[4]*1024, q}'\n"
    )
    admitted = ""
    if host is not None:
        command = shlex.join(("alpha", "agent", "admitted", "--", *host.accounts))
        admitted = (
            f"a=$({program(host)} {command} 2>/dev/null) || a=\n"
            "case \"$a\" in ''|*[!0-9\\ ]*) ;; *) printf '%s\\n' \"$a\";; esac\n"
        )
    return (
        "# tbhbdev-limits\n"
        + PREFIX
        + f'if test "$(uname -s)" = Darwin; then\n{macos}else\n{linux}fi\n'
        + admitted
    )


def preflight(repository: str, ref: str, expected_version: str, host: Host) -> str:
    git_host, owner, project = repo.parts(repository)
    url = f"git@{git_host}:{owner}/{project}.git"
    return (
        PREFIX + f'installed=$({program(host)} --version) || exit "$?"\n'
        "printf '%s\\n' \"$installed\"\n"
        f'test "$installed" = {shlex.quote(expected_version)} || exit 0\n'
        'cd "$HOME" || exit "$?"\n'
        + GIT_SSH
        + f" ls-remote --exit-code -- {shlex.quote(url)} {shlex.quote(ref)} "
        f"{shlex.quote(ref + '^{}')}"
    )
