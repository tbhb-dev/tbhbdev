"""Durable secret save, restore, commit, and interrupted-run recovery."""

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
lazy import fcntl
lazy from pathlib import Path

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.command import CommandError
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.config import Secret, Service, integer, text
from tbhbops.deploy.protocol import Json, RefusalError, decode, mapping, require


class BusyError(RuntimeError):
    """A live receiver owns this service's lock."""


class RecoveryPendingError(RefusalError):
    """The saved service could not restart; a validated release may repair it."""


@dataclass
class Lock:
    state: Path
    descriptor: int | None = None

    @property
    def held(self) -> bool:
        return self.descriptor is not None

    def acquire(self) -> None:
        require(not self.held, "lock already held")
        with fs.parent(self.state / "lock") as (parent, name):
            descriptor = fs.open_entry(name, os.O_RDWR | os.O_CREAT, dir_fd=parent)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if isinstance(error, BlockingIOError):
                message = "live deploy lock"
                raise BusyError(message) from None
            raise
        self.descriptor = descriptor

    def release(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@dataclass
class Transaction:
    service: Service
    engine: Engine

    @property
    def state(self) -> Path:
        return self.service.root / "deploy"

    @property
    def txn(self) -> Path:
        return self.state / "txn"

    def journal(self) -> dict[str, Json] | None:
        try:
            journal = mapping(decode(fs.read(self.txn / "journal.json")))
            require(
                re.fullmatch(r"[0-9a-f]{40}", text(journal["release"])) is not None,
                "journal release name",
            )
            for name in ("current", "previous"):
                if journal[name] is not None:
                    fs.release_name(text(journal[name]))
        except FileNotFoundError:
            return None
        else:
            return journal

    def update(self, journal: dict[str, Json], state: str) -> None:
        journal["state"] = state
        fs.write(
            self.txn / "journal.json", json.dumps(journal, sort_keys=True).encode()
        )

    def delete(self) -> None:
        # unlink and fsync txn/ precede every transaction directory removal.
        fs.unlink(self.txn / "journal.json")
        shutil.rmtree(self.txn)
        fs.sync(self.state)

    def pointer(self, name: str, target: str | None) -> None:
        fs.pointer(self.service.root / name, target)
        fs.sync(self.state)

    def save(self, release: str) -> dict[str, Json]:
        if self.txn.exists():
            require(self.journal() is None, "pending transaction")
            shutil.rmtree(self.txn)
        fs.mkdir(self.txn)
        saved = self.txn / "saved"
        fs.mkdir(saved)
        fs.mkdir(saved / "payload")
        fs.mkdir(saved / "metadata")
        entries: dict[str, Json] = {}
        for secret in self.service.secrets:
            info = fs.inspect(secret.target, file=True, missing=True)
            entry: dict[str, Json] = {"existed": info is not None}
            if info is not None:
                fs.write(saved / "payload" / secret.name, fs.read(secret.target))
                entry.update(
                    mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid
                )
            entries[secret.name] = entry
        for directory in (saved, self.txn, self.state):
            fs.sync(directory)
        fs.write(
            saved / "metadata" / "metadata.json",
            json.dumps(entries, sort_keys=True).encode(),
        )
        journal: dict[str, Json] = {
            "release": release,
            "current": fs.target(self.service.root / "current"),
            "previous": fs.target(self.service.root / "previous"),
            "secrets": entries,
        }
        self.update(journal, "open")
        return journal

    def different(self, secret: Secret, body: bytes) -> bool:
        info = fs.inspect(secret.target, file=True, missing=True)
        return info is None or (
            fs.read(secret.target) != body
            or stat.S_IMODE(info.st_mode) != secret.mode
            or info.st_uid != secret.uid
            or info.st_gid != secret.gid
        )

    def identical(self, release: str, contents: dict[str, bytes]) -> bool:
        return (
            fs.target(self.service.root / "current") == f"releases/{release}"
            and not (self.state / "force-recreate").exists()
            and all(
                not self.different(secret, contents[secret.name])
                for secret in self.service.secrets
            )
        )

    def write(self, contents: dict[str, bytes]) -> bool:
        changed = False
        for secret in self.service.secrets:
            body = contents[secret.name]
            if self.different(secret, body):
                fs.write(secret.target, body, secret.mode, secret.uid, secret.gid)
                changed = True
        return changed

    def write_saved(self) -> bool:
        saved = self.state / "last-secrets"
        entries = mapping(decode(fs.read(saved / "metadata" / "metadata.json")))
        # A rollback may remove targets that the earlier release never had.
        fs.write(self.state / "force-recreate", b"1\n")
        for secret in self.service.secrets:
            entry = mapping(entries[secret.name])
            if entry["existed"] is True:
                fs.write(
                    secret.target,
                    fs.read(saved / "payload" / secret.name),
                    integer(entry["mode"]),
                    integer(entry["uid"]),
                    integer(entry["gid"]),
                )
            else:
                fs.unlink(secret.target)
        return bool(entries)

    def restore(self, journal: dict[str, Json]) -> None:
        entries = mapping(journal["secrets"])
        if entries:
            fs.write(self.state / "force-recreate", b"1\n")
        for secret in self.service.secrets:
            entry = mapping(entries[secret.name])
            if entry["existed"] is True:
                fs.write(
                    secret.target,
                    fs.read(self.txn / "saved" / "payload" / secret.name),
                    integer(entry["mode"]),
                    integer(entry["uid"]),
                    integer(entry["gid"]),
                )
            else:
                fs.unlink(secret.target)
        for name in ("current", "previous"):
            target = journal[name]
            self.pointer(name, None if target is None else text(target))
        self.update(journal, "restored")

    def finish(self, journal: dict[str, Json]) -> None:
        self.update(journal, "committed")
        saved = self.txn / "saved"
        if saved.exists() and journal["current"] != f"releases/{journal['release']}":
            last = self.state / "last-secrets"
            if last.exists():
                shutil.rmtree(last)
            saved.rename(last)
            fs.sync(self.state)
            fs.sync(self.txn)
        self.prune()
        self.delete()

    def prune(self) -> None:
        root = self.service.root
        # Incoming directories are pruned when a run finishes (or before a new
        # stream), never while recovering an open journal.
        for directory in (root / "incoming").iterdir():
            shutil.rmtree(directory)
        keep = {fs.target(root / name) for name in ("current", "previous")}
        for path in self.state.glob("recovery-*/journal.json"):
            journal = mapping(decode(fs.read(path)))
            keep.update(
                text(journal[name])
                for name in ("current", "previous")
                if journal[name] is not None
            )
        releases = sorted(
            (
                path
                for path in (root / "releases").iterdir()
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for release in releases[5:]:
            if f"releases/{release.name}" in keep:
                continue
            shutil.rmtree(release)
            fs.unlink(self.engine.model_path(release.name))
        fs.sync(root / "releases")

    def recover(self) -> bool:
        """Return whether recovery restored a release, satisfying a manual rollback."""
        journal = self.journal()
        if journal is None:
            if self.txn.exists():
                shutil.rmtree(self.txn)
            return False
        if journal["state"] == "committed":
            self.finish(journal)
            return False
        require(journal["state"] in {"open", "restored"}, "journal state")
        self.engine.stop()
        self.restore(journal)
        current = journal["current"]
        if current is not None:
            try:
                self.engine.up(fs.release_name(text(current)), recreate=True)
            except CommandError, OSError, RefusalError, ValueError, KeyError, TypeError:
                message = "recovery still pending"
                raise RecoveryPendingError(message) from None
        else:
            self.engine.remove()
        self.delete()
        return True

    def preserve(self) -> None:
        """Retain the failed recovery before starting a forward repair journal."""
        destination = Path(tempfile.mkdtemp(prefix="recovery-", dir=self.state))
        self.txn.replace(destination)
        fs.sync(self.state)
