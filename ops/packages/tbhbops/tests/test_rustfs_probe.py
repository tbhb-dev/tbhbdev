# cspell:ignore rustfsadmin ENOSYS
"""RustFS classifiers never disclose diagnostic text."""

import pytest
from tbhbops.o11y import rustfs_probe as probe


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("", "console line missing"),
        ('"console_enabled":true', "console line missing"),
        ("console_enabled:false", "console line missing"),
        ("rustfsadmin", "default login"),
        ("default root credentials", "default login"),
        ("is not set", "unset credential"),
        ("ENOSYS", "unsupported host"),
        ("Function not implemented", "unsupported host"),
        ("Unsupported filesystem", "unsupported filesystem"),
    ],
)
@pytest.mark.parametrize("console", ["", '"console_enabled":false\n'])
def test_start_log_refusals(text: str, rule: str, console: str) -> None:
    if text:
        assert (
            probe.start_log(
                (console if rule != "console line missing" else "") + text.swapcase()
            )
            == rule
        )
    else:
        assert probe.start_log(text) == rule


def test_compose_data_dir() -> None:
    assert probe.compose_data_dir(linux=True) is None
    assert probe.compose_data_dir(linux=False) == "/var/lib/o11y-test/probe"
    assert probe.VM_DATA_ROOT == "/var/lib/o11y-test"
    assert probe.VM_DATA_NAME == "probe"


def test_clean_log() -> None:
    assert probe.start_log('noise\n{"console_enabled":false}\n') is None
