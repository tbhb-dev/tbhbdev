"""Text safe to display in a terminal."""

import unicodedata


def terminal_text(text: str) -> str:
    return "".join(
        c
        for c in text
        if c in {"\n", "\t"} or not unicodedata.category(c).startswith("C")
    )
