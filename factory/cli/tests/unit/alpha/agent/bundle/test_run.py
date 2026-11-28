"""The bundled run entry point refuses until implemented."""

import pytest

from tbhbdev.alpha.agent.bundle import run
from tbhbdev.alpha.agent.model import Invalid


def test_run_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        run.main([], "tbhbdev alpha agent run")
