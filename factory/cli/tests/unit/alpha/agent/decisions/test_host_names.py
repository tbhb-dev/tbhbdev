"""Host name matching uses only the supplied name and set."""

import pytest

from tbhbdev.alpha.agent.decisions.host_names import contains_host_name


@pytest.mark.parametrize(
    ("name", "known_hosts", "found"),
    [
        ("examplehost01", {"examplehost01"}, True),
        ("prepare-EXAMPLEHOST01build", {"examplehost01", "examplehost02"}, True),
        ("agent/prepare-examplehost02", {"ExampleHost02"}, True),
        ("agent/prepare-examplehost", {"examplehost01", "examplehost02"}, False),
        ("examplehost01", set(), False),
    ],
)
def test_contains_host_name(name: str, known_hosts: set[str], found: bool) -> None:
    assert contains_host_name(name, known_hosts) is found
