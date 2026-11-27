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


def test_clean_log_and_wording() -> None:
    assert probe.start_log('noise\n{"console_enabled":false}\n') is None
    assert probe.CRASH_FAILURE == (
        "an acknowledged write was lost or a listed key is unreadable after a kill"
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "probe: 0 acknowledged writes survived",
        "probe: -1 acknowledged writes survived",
        "secret probe: 5 acknowledged writes survived",
        "probe: 5 acknowledged writes survived secret",
        "probe: 5 acknowledged writes survived\nprobe: 6 acknowledged writes survived",
    ],
)
def test_invalid_survivors(text: str) -> None:
    assert probe.survivors(text) is None


def test_survivors_only_returns_count() -> None:
    assert (
        probe.survivors("private\nprobe: 005 acknowledged writes survived\nprivate")
        == "5 acknowledged writes survived"
    )
    assert probe.survivors("probe: 1 acknowledged writes survived") == (
        "1 acknowledged writes survived"
    )


@pytest.mark.parametrize("name", ["", "..", "a..b", "../a", "/a", "a/b"])
def test_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match=r"^unsafe probe basename$"):
        probe.basename(name)


def test_safe_names() -> None:
    assert probe.basename("data") == "data"
    assert probe.basename("rustfs") == "rustfs"
