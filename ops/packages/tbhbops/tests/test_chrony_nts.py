"""Keep the NTS fragment exact and every existing time source intact."""

import base64
from pathlib import Path

import jinja2
import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).resolve().parents
    if (path / "ansible" / "roles").is_dir()
)
ROLE = ANSIBLE / "roles" / "chrony_nts"
EXPECTED = (
    "# Managed by ansible role chrony_nts\nserver time.cloudflare.com iburst nts\n"
)
# Ubuntu 24.04's stock debian/chrony.conf from the ubuntu/noble source branch.
STOCK_UBUNTU_CONFIG = """\
# Welcome to the chrony configuration file. See chrony.conf(5) for more
# information about usable directives.

# Include configuration files found in /etc/chrony/conf.d.
confdir /etc/chrony/conf.d

# This will use (up to):
# - 4 sources from ntp.ubuntu.com which some are ipv6 enabled
# - 2 sources from 2.ubuntu.pool.ntp.org which is ipv6 enabled as well
# - 1 source from [01].ubuntu.pool.ntp.org each (ipv4 only atm)
# This means by default, up to 6 dual-stack and up to 2 additional IPv4-only
# sources will be used.
# At the same time it retains some protection against one of the entries being
# down (compare to just using one of the lines). See (LP: #1754358) for the
# discussion.
#
# About using servers from the NTP Pool Project in general see (LP: #104525).
# Approved by Ubuntu Technical Board on 2011-02-08.
# See http://www.pool.ntp.org/join.html for more information.
pool ntp.ubuntu.com        iburst maxsources 4
pool 0.ubuntu.pool.ntp.org iburst maxsources 1
pool 1.ubuntu.pool.ntp.org iburst maxsources 1
pool 2.ubuntu.pool.ntp.org iburst maxsources 2

# Use time sources from DHCP.
sourcedir /run/chrony-dhcp

# Use NTP sources found in /etc/chrony/sources.d.
sourcedir /etc/chrony/sources.d

# This directive specify the location of the file containing ID/key pairs for
# NTP authentication.
keyfile /etc/chrony/chrony.keys

# This directive specify the file into which chronyd will store the rate
# information.
driftfile /var/lib/chrony/chrony.drift

# Save NTS keys and cookies.
ntsdumpdir /var/lib/chrony

# Uncomment the following line to turn logging on.
#log tracking measurements statistics

# Log files location.
logdir /var/log/chrony

# Stop bad estimates upsetting machine clock.
maxupdateskew 100.0

# This directive enables kernel synchronisation (every 11 minutes) of the
# real-time clock. Note that it can't be used along with the 'rtcfile' directive.
rtcsync

# Step the system clock instead of slewing it if the adjustment is larger than
# one second, but only in the first three clock updates.
makestep 1 3

# Get TAI-UTC offset and leap seconds from the system tz database.
# This directive must be commented out when using time sources serving
# leap-smeared time.
leapsectz right/UTC
"""


def test_template_renders_exactly_two_lines_with_a_final_newline() -> None:
    environment = jinja2.Environment(  # noqa: S701 - chrony text, not HTML
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    assert environment.get_template("10-cloudflare-nts.conf.j2").render() == EXPECTED
    assert sorted(path.name for path in (ROLE / "templates").iterdir()) == [
        "10-cloudflare-nts.conf.j2"
    ]


def test_role_only_writes_the_fragment_and_only_reads_the_main_configuration() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    assert len(tasks) == 7
    assert tasks[0]["ansible.builtin.stat"] == {"path": "/etc/chrony/conf.d"}
    assert tasks[2]["ansible.builtin.slurp"] == {"src": "/etc/chrony/chrony.conf"}
    assert set(tasks[1]) == {"name", "ansible.builtin.assert"}
    assert set(tasks[3]) == {"name", "ansible.builtin.assert"}
    assert tasks[4] == {
        "name": "Write the Cloudflare NTS configuration",
        "ansible.builtin.template": {
            "src": "10-cloudflare-nts.conf.j2",
            "dest": "/etc/chrony/conf.d/10-cloudflare-nts.conf",
            "owner": "root",
            "group": "root",
            "mode": "0644",
        },
        "notify": "Restart chrony",
    }
    assert tasks[5] == {
        "name": "Run the chrony restart handler",
        "ansible.builtin.meta": "flush_handlers",
    }
    # The required read-only check is the sole main-configuration reference.
    assert "chrony.conf" not in repr(tasks[:2] + tasks[3:])
    for path in ROLE.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "sources.d" not in text
            assert "canonical" not in text.lower()


@pytest.mark.parametrize(
    "stat", [{}, {"exists": False}, {"isdir": False}, {"isdir": True}]
)
def test_check_requires_an_existing_directory(stat: dict[str, bool]) -> None:
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks/main.yml"), trusted_as_template=True
    )
    templar = Templar(loader=loader, variables={"chrony_nts_confdir": {"stat": stat}})
    guard = tasks[1]["ansible.builtin.assert"]["that"][0]
    assert templar.evaluate_expression(guard) is stat.get("isdir", False)


@pytest.mark.parametrize(
    ("configuration", "accepted"),
    [
        (STOCK_UBUNTU_CONFIG, True),
        ("confdir /etc/chrony/conf.d\n", True),
        ("  confdir\t/etc/chrony/conf.d/ # existing fragments\n", True),
        ("include /etc/chrony/conf.d\n", False),
        ("include /etc/chrony/conf.d/*.conf\n", True),
        ("pool example.net iburst\n confdir /etc/chrony/conf.d\n", True),
        ("", False),
        ("# confdir /etc/chrony/conf.d\n", False),
        ("! include /etc/chrony/conf.d\n", False),
        ("confdir /etc/chrony/other\n", False),
        ("confdir /etc/chrony/conf.d-disabled\n", False),
        ("confdir /etc/chrony/conf.d extra\n", False),
        ("include /etc/chrony/conf.d/other.conf\n", False),
        ("include /etc/chrony/conf.d/*.sources\n", False),
        ("server example.net # confdir /etc/chrony/conf.d\n", False),
    ],
)
@pytest.mark.parametrize("check_mode", [False, True])
def test_check_requires_an_active_include_even_in_check_mode(
    configuration: str, accepted: bool, check_mode: bool
) -> None:
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks/main.yml"), trusted_as_template=True
    )
    encoded = base64.b64encode(configuration.encode()).decode()
    templar = Templar(
        loader=loader,
        variables={
            "chrony_nts_config": {"content": encoded},
            "ansible_check_mode": check_mode,
        },
    )
    assert (
        templar.evaluate_expression(tasks[3]["ansible.builtin.assert"]["that"][0])
        is accepted
    )
    for task in tasks[:4]:
        assert "when" not in task
        assert "check_mode" not in task


@pytest.mark.parametrize("run", [False, True])
@pytest.mark.parametrize("check_mode", [False, True])
def test_service_check_runs_without_a_file_write_or_check_mode_override(
    run: bool, check_mode: bool
) -> None:
    loader = DataLoader()
    tasks = loader.load_from_file(
        str(ROLE / "tasks/main.yml"), trusted_as_template=True
    )
    task = tasks[6]
    assert task == {
        "name": "Ensure chrony is running",
        "ansible.builtin.systemd_service": {"name": "chrony", "state": "started"},
        "when": "chrony_nts_run",
    }
    templar = Templar(
        loader=loader,
        variables={"chrony_nts_run": run, "ansible_check_mode": check_mode},
    )
    assert templar.evaluate_expression(task["when"]) is run


@pytest.mark.parametrize("run", [False, True])
@pytest.mark.parametrize("check_mode", [False, True])
def test_handler_restarts_only_when_notified_outside_check_mode(
    run: bool, check_mode: bool
) -> None:
    loader = DataLoader()
    handlers = loader.load_from_file(
        str(ROLE / "handlers/main.yml"), trusted_as_template=True
    )
    assert len(handlers) == 1
    handler = handlers[0]
    assert handler["name"] == "Restart chrony"
    assert handler["ansible.builtin.systemd_service"] == {
        "name": "chrony",
        "state": "restarted",
    }
    templar = Templar(
        loader=loader,
        variables={"chrony_nts_run": run, "ansible_check_mode": check_mode},
    )
    assert templar.evaluate_expression(handler["when"]) is (run and not check_mode)


def test_play_targets_the_companion_inventory_group() -> None:
    plays = yaml.safe_load(
        (ANSIBLE / "playbooks/lab-chrony-nts.yml").read_text(encoding="utf-8")
    )
    assert len(plays) == 1
    assert plays[0]["hosts"] == "chrony_nts_hosts"
    assert plays[0]["become"] is True
    assert plays[0]["gather_facts"] is False
    assert plays[0]["roles"] == [{"role": "chrony_nts", "tags": ["chrony_nts"]}]
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert defaults == {"chrony_nts_run": True}
