"""Plan protected paths from path spellings alone."""

from pathlib import PurePosixPath

import pytest

from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.run_host import account_paths


def test_protected_paths_include_startup_and_socket_parents() -> None:
    home = PurePosixPath("/example/home")
    paths = account_paths.protected_home_paths(home, ".control/sockets/socket")
    assert home / ".zshenv" in paths
    assert home / ".control/sockets" in paths
    assert home / ".control" in paths
    assert home not in paths


@pytest.mark.parametrize(
    "root", ["/example", "/example/protected", "/example/protected/child"]
)
def test_overlap_is_refused_in_either_direction(root: str) -> None:
    with pytest.raises(Invalid, match="overlaps write root"):
        account_paths.protect_paths((root,), ("/example/protected",))
    account_paths.protect_paths(
        ("/example/protected-sibling",), ("/example/protected",)
    )
