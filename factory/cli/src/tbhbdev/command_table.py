# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Lazy mapping from command names to entry-point callables."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import override
lazy from collections.abc import Callable, Iterator, Mapping

type Command = Callable[[list[str], str], int]
type Loader = Callable[[], Command]


class CommandTable(MutableMapping[str, Command]):
    """Resolve each command entry point only when it is looked up."""

    def __init__(self, loaders: Mapping[str, Loader]) -> None:
        self._loaders: dict[str, Loader] = dict(loaders)
        self._overrides: dict[str, Command] = {}

    @override
    def __contains__(self, key: object) -> bool:
        return key in self._overrides or key in self._loaders

    @override
    def __getitem__(self, key: str) -> Command:
        if key in self._overrides:
            return self._overrides[key]
        return self._loaders[key]()

    @override
    def __setitem__(self, key: str, value: Command) -> None:
        self._overrides[key] = value

    @override
    def __delitem__(self, key: str) -> None:
        del self._overrides[key]

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._loaders | self._overrides)

    @override
    def __len__(self) -> int:
        return len(self._loaders.keys() | self._overrides.keys())
