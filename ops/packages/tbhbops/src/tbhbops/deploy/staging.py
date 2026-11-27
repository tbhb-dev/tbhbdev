"""Prepare the service root and publish or clean up release files."""

import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.config import Service
from tbhbops.deploy.protocol import Release, require

ROOT_MODE = 0o700


def prepare(service: Service) -> None:
    info = fs.inspect(service.root)
    require(
        info is not None
        and stat.S_ISDIR(info.st_mode)
        and info.st_uid == 0
        and stat.S_IMODE(info.st_mode) == ROOT_MODE,
        "service root ownership or mode",
    )
    for name in ("deploy", "incoming", "releases"):
        fs.mkdir(service.root / name)
    fs.mkdir(service.root / "deploy" / "models")
    if service.init:
        executable = fs.inspect(Path(service.init[0]), file=True)
        require(
            executable is not None and executable.st_uid == 0,
            "init executable owner",
        )


@dataclass
class Staging:
    incoming: Path | None = None
    created: bool = False

    def publish(self, service: Service, release_name: str, release: Release) -> None:
        destination = service.root / "releases" / release_name
        retained = sum(
            path.stat().st_size
            for path in (service.root / "releases").rglob("*")
            if path.is_file()
        )
        added = (
            0
            if destination.exists()
            else sum(len(body) for body in release.files.values())
            + len(release.digest.encode())
        )
        require(
            retained + added <= service.max_retained_mib * 1024**2,
            "retained bytes limit",
        )
        self.incoming = Path(tempfile.mkdtemp(dir=service.root / "incoming"))
        tree = self.incoming / "release"
        fs.mkdir(tree, 0o755)
        for name, body in release.files.items():
            path = tree / name.removeprefix("release/")
            for directory in reversed(path.parents):
                if directory == tree or tree in directory.parents:
                    fs.mkdir(directory, 0o755)
            fs.write(path, body, 0o644)
        # The release SHA is a caller label, not a content digest. Root-owned
        # published trees are immutable by contract. Retries trust their saved
        # digest marker rather than hashing the published content again.
        digest = destination / ".sha256"
        fs.write(tree / ".sha256", release.digest.encode(), 0o644)
        if destination.exists():
            require(
                fs.inspect(digest, file=True, missing=True) is not None,
                "published release missing digest",
            )
            require(
                fs.read(digest).decode() == release.digest, "release digest conflict"
            )
        else:
            tree.rename(destination)
            self.created = True
            fs.sync(destination.parent)

    def cleanup(
        self, destination: Path, model_path: Path, *, before_secrets: bool
    ) -> None:
        if self.incoming is not None and self.incoming.exists():
            shutil.rmtree(self.incoming)
        if before_secrets and self.created:
            shutil.rmtree(destination)
            fs.unlink(model_path)
