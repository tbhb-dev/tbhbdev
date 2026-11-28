"""Bundle transfer refuses until implemented."""

import pytest
from agent_example import LINUX, settings
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.driver.decisions.metadata import Run
from tbhbdev.alpha.agent.launch import ship
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.transport import Inputs


def test_send_refuses() -> None:
    run = Run("run", LINUX, request.parse(BUILD), "", "owner")
    inputs = Inputs("{}", "brief", None, run.owner)

    with pytest.raises(Invalid, match=r"^not built$"):
        ship.send(settings(), LINUX, run, b"", inputs, 1.0)
