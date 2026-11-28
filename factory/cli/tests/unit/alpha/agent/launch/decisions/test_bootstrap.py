"""Bootstrap commands refuse until implemented."""

import pytest
from agent_example import LINUX

from tbhbdev.alpha.agent.launch.decisions import bootstrap
from tbhbdev.alpha.agent.model import Invalid, Profile


def test_receive_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        bootstrap.receive("run", "sockets", "0" * 64, "python3")


def test_start_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        bootstrap.start("run", "sockets", LINUX.name)


def test_check_slot_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        bootstrap.check_slot("run", LINUX, Profile.BUILD)


def test_remove_cli_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        bootstrap.remove_cli("run")
