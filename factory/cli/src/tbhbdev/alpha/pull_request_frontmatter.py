# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parse YAML frontmatter and report validation findings for a draft."""

import re
from typing import TYPE_CHECKING, override

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

FRONTMATTER = re.compile(r"---\n(?P<yaml>.*?)^---[ \t]*$\n?", re.DOTALL | re.MULTILINE)


class FrontmatterError(ValueError):
    """The frontmatter block cannot be read."""


def keep_text(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    """Construct a timestamp as the text the author wrote."""
    return loader.construct_scalar(node)  # pyrefly: ignore[bad-argument-type]


class Loader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as text and refuses duplicate keys."""

    @override
    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        """Refuse a key that appears twice, which plain YAML resolves silently."""
        keys = [self.construct_object(key, deep=True) for key, _ in node.value]
        for key in keys:
            if keys.count(key) > 1:
                message = f"duplicate key {key!r}"
                raise yaml.constructor.ConstructorError(
                    None, None, message, node.start_mark
                )
        return super().construct_mapping(node, deep)


Loader.add_constructor("tag:yaml.org,2002:timestamp", keep_text)


def parse_frontmatter(text: str) -> tuple[object, str]:
    """Split a draft into its parsed frontmatter (None when absent) and body.

    Raises:
        FrontmatterError: The block is unterminated or is not valid YAML.
    """
    match = FRONTMATTER.match(text)
    if match is None:
        if text.startswith("---\n"):
            message = "unterminated frontmatter"
            raise FrontmatterError(message)
        return None, text
    try:
        # Loader is a SafeLoader.
        data: object = yaml.load(match["yaml"], Loader=Loader)  # noqa: S506  # nosec B506
    except yaml.YAMLError as exc:
        message = f"invalid YAML: {' '.join(str(exc).split())}"
        raise FrontmatterError(message) from exc
    return data, text[match.end() :]


def meta_problems(
    data: object, offset: int, text: str, validator: Draft202012Validator
) -> list[tuple[int, str]]:
    """Validate the frontmatter, pointing each error at its key's line."""
    if data is None:
        return []
    frontmatter = text.splitlines()[:offset]
    return [
        (
            key_line(error, frontmatter),
            f"frontmatter {error.json_path}: {error.message}",
        )
        for error in sorted(
            validator.iter_errors(data), key=lambda found: found.json_path
        )
    ]


def key_line(error: ValidationError, frontmatter: Sequence[str]) -> int:
    """The line of the top-level key an error is about; 1 when it has none."""
    if not error.absolute_path:
        return 1
    key = f"{error.absolute_path[0]}:"
    return next(
        (index for index, line in enumerate(frontmatter, 1) if line.startswith(key)), 1
    )
