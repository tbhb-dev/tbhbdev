"""Run the version, repository, and resource probes in a shell."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
lazy from pathlib import Path

import pytest
from agent_driver_example import write_standalone_fake
from agent_example import LINUX
from remote_script_example import execute

from tbhbdev.alpha.agent.decisions import (
    limits,
    remote_host,
)


def test_preflight_ref_contract(host: Path) -> None:
    write_standalone_fake(host / "bin/version", "print('example version')\n")
    write_standalone_fake(
        host / "bin/git",
        "import os, sys\nfrom pathlib import Path\n"
        f"assert Path.cwd() == Path({str(host)!r})\n"
        "assert sys.argv[-6:] == ['ls-remote', '--exit-code', '--',\n"
        "    'git@github.com:example-org/example-repo.git', 'topic', 'topic^{}']\n"
        "assert 'protocol.ext.allow=never' in sys.argv\n"
        "assert os.environ['GIT_NO_LAZY_FETCH'] == '1'\n"
        "assert os.environ['LANG'] == os.environ['LC_ALL'] == 'C'\n"
        "print('commit\\ttopic')\n",
    )
    config = replace(LINUX, tbhbdev=str(host / "bin/version"))
    script = remote_host.preflight(
        "example-org/example-repo", "topic", "example version", config
    )
    result = execute(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"example version\ncommit\ttopic\n"


def test_script_prints_the_numbers_of_this_host() -> None:
    script = remote_host.limits()
    assert script.startswith("# tbhbdev-limits\n")
    done = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=True
    )
    assert isinstance(limits.parse(done.stdout), limits.Reading)


@pytest.mark.parametrize(
    "key", ["vm.loadavg", "hw.logicalcpu", "hw.memsize", "vm.swapusage"]
)
def test_a_failed_macos_read_ends_the_script_nonzero(key: str) -> None:
    script = remote_host.limits()
    lines = [x for x in script.splitlines() if f"sysctl -n {key})" in x]
    assert len(lines) == 1
    assert lines[0].endswith("|| exit 1")
    if sys.platform == "darwin":
        broken = script.replace(f"sysctl -n {key}", "false")
        done = subprocess.run(
            ["/bin/sh", "-c", broken], capture_output=True, text=True, check=False
        )
        assert (done.returncode, done.stdout) == (1, "")


def run_script(script: str) -> tuple[int, str]:
    done = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=False
    )
    return done.returncode, done.stdout


@pytest.mark.parametrize(
    "text", ["read l _ < /proc/loadavg", "c=$(nproc)", "v=$(vm_stat)"]
)
def test_every_other_read_ends_the_script_nonzero_too(text: str) -> None:
    lines = [x for x in remote_host.limits().splitlines() if text in x]
    assert len(lines) == 1
    assert lines[0].endswith("|| exit 1")


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/proc/loadavg", "/nonexistent"),
        ("$(nproc)", "$(false)"),
    ],
)
def test_a_failed_linux_read_ends_the_script_nonzero(old: str, new: str) -> None:
    script = remote_host.limits().replace(old, new)
    assert run_script(script) == (1, "")


MEMINFO = {
    "MemTotal": "10 kB",
    "MemAvailable": "5 kB",
    "SwapTotal": "0 kB",
    "SwapFree": "0 kB",
}


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
@pytest.mark.parametrize("field", list(MEMINFO))
@pytest.mark.parametrize("damage", ["missing", "empty", "word"])
def test_each_linux_memory_field_must_be_a_number(
    field: str, damage: str, tmp_path: Path
) -> None:
    lines = {
        name: {"missing": None, "empty": f"{name}:", "word": f"{name}: x kB"}[damage]
        if name == field
        else f"{name}: {value}"
        for name, value in MEMINFO.items()
    }
    (tmp_path / "meminfo").write_text(
        "".join(f"{line}\n" for line in lines.values() if line)
    )
    script = remote_host.limits().replace("/proc/meminfo", str(tmp_path / "meminfo"))
    assert run_script(script) == (1, "")


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_a_swapless_linux_host_with_zero_fields_is_readable(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(
        "".join(f"{name}: {value}\n" for name, value in MEMINFO.items())
    )
    script = remote_host.limits().replace("/proc/meminfo", str(tmp_path / "meminfo"))
    code, out = run_script(script)
    assert code == 0
    reading = limits.parse(out)
    assert isinstance(reading, limits.Reading)
    assert reading.memory_available == 5


def macos_script(level: str) -> str:
    fake = f"""\
sysctl() {{
    case "$2" in
        vm.loadavg) echo '{{ 0.5 1 2 }}';;
        hw.logicalcpu) echo '8';;
        hw.memsize) echo '8589934592';;
        vm.swapusage) echo 'total = 4.00M  used = 1.00M  free = 3.00M';;
        kern.memorystatus_vm_pressure_level) echo '{level}';;
    esac
}}
vm_stat() {{
    printf '%s\\n' 'Mach Virtual Memory Statistics: (page size of 16384 bytes)' \
        'Pages free: 10.' 'Pages inactive: 20.' 'Pages speculative: 3.' \
        'Pages purgeable: 2.' 'Pages active: 100.'
}}
"""
    return fake + remote_host.limits().replace("$(uname -s)", "Darwin")


@pytest.mark.parametrize(
    ("answer", "expected"), [("2", 2.0), ("4", 4.0), ("", None), ("x", None)]
)
def test_macos_reads_four_page_counts_and_the_pressure_level(
    answer: str, expected: float | None
) -> None:
    code, out = run_script(macos_script(answer))
    assert code == 0
    assert limits.parse(out) == limits.Reading(
        0.5, 8, 8388608, 560, 4096, 1024, expected
    )


PRESSURE = (
    "some avg10=%s avg60=0.10 avg300=0.00 total=1\n"
    "full avg10=0.00 avg60=0 avg300=0 total=0\n"
)


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (PRESSURE % "12.50", 12.5),
        (PRESSURE % "0.00", 0.0),
        ("full avg10=3.00 avg60=0 avg300=0 total=0\n", None),
        ("some garbage\n", None),
        (None, None),
    ],
)
def test_linux_pressure_is_some_avg10_or_unknown(
    text: str | None, expected: float | None, tmp_path: Path
) -> None:
    (tmp_path / "meminfo").write_text(
        "".join(f"{name}: {value}\n" for name, value in MEMINFO.items())
    )
    script = remote_host.limits().replace("/proc/meminfo", str(tmp_path / "meminfo"))
    if text is None:
        script = script.replace("/proc/pressure/memory", str(tmp_path / "absent"))
    else:
        (tmp_path / "pressure").write_text(text)
        script = script.replace("/proc/pressure/memory", str(tmp_path / "pressure"))
    code, out = run_script(script)
    assert code == 0
    found = limits.parse(out)
    assert isinstance(found, limits.Reading)
    assert found.pressure == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="reads sysctl and vm_stat")
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("$(vm_stat)", "$(echo 'Pages free: 5.'; false)"),
        (
            "$(vm_stat)",
            "$(echo 'Mach Virtual Memory Statistics: (page size of 16384 bytes)')",
        ),
        ("sysctl -n vm.swapusage", "echo 'total =  used = 5.00M  free = 1.00M'"),
        ("sysctl -n vm.swapusage", "echo 'garbage'"),
    ],
)
def test_a_partial_macos_read_ends_the_script_nonzero(old: str, new: str) -> None:
    assert run_script(remote_host.limits().replace(old, new)) == (1, "")
