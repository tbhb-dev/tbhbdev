"""Gather the running CLI installation for packing on the driver."""

from importlib import metadata
from pathlib import Path

from tbhbdev import version
from tbhbdev.alpha.agent.decisions.cli_bundle import pack

PACKAGE = Path(__file__).parents[2]


def _raise_walk_error(error: OSError) -> None:
    raise error


def _files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for folder, directories, names in root.walk(on_error=_raise_walk_error):
        if "__pycache__" in directories:
            directories.remove("__pycache__")
        for name in names:
            if not name.endswith(".pyc") and name != "__pycache__":
                path = folder / name
                files[path.relative_to(root.parent).as_posix()] = path.read_bytes()
    return files


def installed() -> tuple[bytes, str, str]:
    """Return the running CLI's bundle, build commit and bundle SHA-256."""
    dist = metadata.distribution(version.NAME)
    commit = version.installed_identity(dist)
    info = Path(str(dist.locate_file(f"{version.NAME}-{dist.version}.dist-info")))
    pins = {item.name: item.version for item in metadata.distributions()}
    bundle, digest = pack(
        _files(PACKAGE), _files(info), identity=commit, distributions=pins
    )
    return bundle, commit, digest
