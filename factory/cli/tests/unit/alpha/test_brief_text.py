"""Text normalization for brief checks."""

from tbhbdev.alpha.brief_text import normalize


def test_normalize_unicode_whitespace_and_case() -> None:
    assert (
        normalize("  \uff26u\N{LATIN SMALL LETTER SHARP S}\N{NO-BREAK SPACE}\n\t X  ")
        == "fuss x"
    )
    assert normalize("") == ""
