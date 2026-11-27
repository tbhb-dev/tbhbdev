"""Extract host mount sources and enforce release path boundaries."""

import re
from dataclasses import dataclass
lazy from pathlib import Path

from tbhbops.deploy.config import Service, rows, text
from tbhbops.deploy.filesystem import spelling
from tbhbops.deploy.model_fields import typed
from tbhbops.deploy.protocol import Json, mapping, require


@dataclass(frozen=True)
class Source:
    path: Path
    writable: bool = False
    file: bool = False
    env: bool = False


def _volume(value: Json, named: dict[str, Json]) -> Source | None:
    volume = mapping(
        value, {"type", "source", "target", "read_only", "bind", "volume", "tmpfs"}
    )
    kind = text(volume.get("type"))
    require(kind in {"bind", "volume", "tmpfs"}, "mount type")
    text(volume.get("target"))
    if "read_only" in volume:
        typed(volume["read_only"], bool)
    for key, fields in (
        ("bind", {"create_host_path"}),
        ("volume", {"nocopy"}),
        ("tmpfs", {"size", "mode"}),
    ):
        if key in volume:
            require(key == kind, "mount option type")
            for item, setting in mapping(volume[key], fields).items():
                typed(setting, int if item in {"size", "mode"} else bool)
    if kind == "bind":
        return Source(
            spelling(text(volume.get("source"))),
            not bool(volume.get("read_only")),
        )
    if kind == "volume":
        require(text(volume.get("source")) in named, "foreign volume")
    else:
        require("source" not in volume, "tmpfs source")
    return None


def service_sources(key: str, value: Json, model: dict[str, Json]) -> list[Source]:
    paths: list[Source] = []
    for entry in rows(value):
        if key == "env_file":
            mapping(entry, {"path", "required"})
            require(entry.get("required", True) is True, "optional env file")
            paths.append(Source(spelling(text(entry.get("path"))), file=True, env=True))
        else:
            source = _volume(entry, mapping(model.get("volumes", {})))
            if source is not None:
                paths.append(source)
    return paths


def file_sources(model: dict[str, Json]) -> list[Source]:
    paths: list[Source] = []
    for kind in ("secrets", "configs"):
        for entry in mapping(model.get(kind, {})).values():
            file = mapping(entry, {"name", "file"})
            if "name" in file:
                text(file["name"])
            paths.append(Source(spelling(text(file.get("file"))), file=True))
    return paths


def retained(value: Json) -> tuple[Source, ...]:
    """Read host paths from a model validated by its original policy."""
    result = mapping(value)
    paths: list[Source] = []
    for entry in mapping(result.get("services")).values():
        service = mapping(entry)
        paths.extend(
            Source(spelling(text(row.get("path"))), file=True, env=True)
            for row in rows(service.get("env_file", []))
        )
        paths.extend(
            Source(spelling(text(row.get("source"))), not bool(row.get("read_only")))
            for row in rows(service.get("volumes", []))
            if row.get("type") == "bind"
        )
    for kind in ("secrets", "configs"):
        paths.extend(
            Source(spelling(text(mapping(entry).get("file"))), file=True)
            for entry in mapping(result.get(kind, {})).values()
        )
    return tuple(paths)


def paths(
    sources: tuple[Source, ...],
    retained: tuple[Source, ...],
    writable: set[Path],
    allowed: Service,
    release: str,
) -> None:
    """Check lexical and cross-release rules before descriptor-based inspection."""
    root = allowed.root
    own = root / "releases" / release
    secrets = {secret.target for secret in allowed.secrets}
    controls = {
        root / "deploy",
        root / "incoming",
        root / "releases",
        *secrets,
        *(path.parent for path in secrets),
        *allowed.ro_binds,
        *(path.parent for path in allowed.ro_binds),
    }
    # current and previous under root are symlinks into releases/. ro_binds
    # excludes deploy, incoming and releases, but not those two aliases. Docker
    # resolves bind sources; our descriptor mount check refuses symlinks before
    # an alias can bypass the release rules above.
    candidate_writes = {source.path for source in sources if source.writable}
    for source in sources:
        path = source.path
        require(
            re.fullmatch(r"/[A-Za-z0-9._/-]+", str(path)) is not None, "path characters"
        )
        require(
            root in path.parents
            and all(part not in {".", ".."} for part in path.parts),
            "path outside root",
        )
        require(
            not any(
                path.is_relative_to(entry) and not (source.writable and path == entry)
                for entry in writable
            ),
            "path under writable bind",
        )
        if source.writable:
            require(
                not any(
                    path.is_relative_to(control) or control.is_relative_to(path)
                    for control in controls
                ),
                "writable control path",
            )
            require(
                not any(
                    old.path.is_relative_to(path)
                    and not (old.writable and old.path == path)
                    for old in retained
                ),
                "writable retained path",
            )
        else:
            if source.env:
                require(
                    path in secrets or path.is_relative_to(own), "unlisted env file"
                )
            require(
                not path.is_relative_to(root / "deploy")
                and not path.is_relative_to(root / "incoming"),
                "read-only control path",
            )
            require(
                not path.is_relative_to(root / "releases") or path.is_relative_to(own),
                "foreign release path",
            )
            require(
                path.is_relative_to(own) or path in secrets or path in allowed.ro_binds,
                "unlisted read-only source",
            )
    require(candidate_writes <= writable, "incomplete writable set")
