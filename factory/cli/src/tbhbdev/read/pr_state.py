"""The field values of a pull request's REST detail, and their escaping.

Pure: no I/O. `select` turns the JSON of `GET /repos/{owner}/{repo}/pulls/{number}`
into text values, and `escape` makes one value safe for a tab-separated line.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Each field's path into the JSON, in the default output order.
PATHS: dict[str, tuple[str, ...]] = {
    "head_sha": ("head", "sha"),
    "state": ("state",),
    "draft": ("draft",),
    "merge_state": ("mergeable_state",),
    "author": ("user", "login"),
    "title": ("title",),
}
DEFAULT = tuple(PATHS)

_ESCAPES = {
    ord("\\"): "\\\\",
    ord("\t"): "\\t",
    ord("\n"): "\\n",
    ord("\r"): "\\r",
}
# The other characters that str.splitlines() breaks a line at.
_ESCAPES.update(
    {
        code: f"\\u{code:04x}"
        for code in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)
    }
)


class MalformedError(ValueError):
    """The JSON isn't a pull request with fields of the expected types."""


def _find(data: object, field: str) -> object:
    node = data
    for key in PATHS[field]:
        if node is None:
            return None
        if not isinstance(node, dict):
            message = f"{field}: expected an object on the way to {key!r}"
            raise MalformedError(message)
        node = node.get(key)
    return node


def select(data: object, names: Sequence[str]) -> list[str | None]:
    """The text of each named field, None where GitHub gives none.

    Raises:
        MalformedError: DATA isn't an object, or a field has the wrong type.
    """
    if not isinstance(data, dict):
        message = "expected a JSON object"
        raise MalformedError(message)
    values: list[str | None] = []
    for name in names:
        value = _find(data, name)
        if value is not None and not isinstance(
            value, bool if name == "draft" else str
        ):
            message = f"{name}: unexpected {type(value).__name__}"
            raise MalformedError(message)
        values.append(str(value).lower() if isinstance(value, bool) else value)
    return values


def escape(value: str | None) -> str:
    r"""One field of a record.

    Backslash, tab and line breaks are escaped. An absent value is `none`, the
    text `none` is `\\none`, and the empty string stays empty.
    """
    if value is None:
        return "none"
    if value == "none":
        return "\\none"
    return value.translate(_ESCAPES)
