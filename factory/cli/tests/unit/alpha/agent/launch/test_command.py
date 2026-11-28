"""The launch entry point refuses until implemented."""

import pytest

from tbhbdev.alpha.agent.launch import command
from tbhbdev.alpha.agent.model import Invalid


def test_launch_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        command.main([], "tbhbdev alpha agent launch")
