"""Bundle paths refuse until implemented."""

import pytest

from tbhbdev.alpha.agent.bundle import home
from tbhbdev.alpha.agent.model import Invalid


def test_root_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        home.root()


def test_program_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        home.program()


def test_bin_dir_refuses() -> None:
    with pytest.raises(Invalid, match=r"^not built$"):
        home.bin_dir()
