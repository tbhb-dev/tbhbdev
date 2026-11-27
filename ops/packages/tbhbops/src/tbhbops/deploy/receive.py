"""The root receiver validates models before it starts services.

The commit record marks success. A crash after health passes but before that
record restores the previous release on the next receive.
"""

import json
import os
import re
import selectors
import shutil
import signal
import stat
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.config import Service, load, service, text
from tbhbops.deploy.engine import (
    VERSION,
    Command,
    CommandError,
    Engine,
    verify_binary,
)
from tbhbops.deploy.protocol import (
    RefusalError,
    Release,
    Request,
    mapping,
    request,
    require,
    unpack,
)
from tbhbops.deploy.transaction import (
    BusyError,
    Lock,
    RecoveryPendingError,
    Transaction,
)

ROOT_MODE = 0o700
CONFIG = Path("/etc/tbhb/deploy")
EMPTY_REQUEST = ""


def stream(limit: int) -> bytes:
    """Wait for EOF for at most 300 seconds, with exactly one framed tar stream."""
    deadline = time.monotonic() + 300
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            require(
                remaining > 0 and bool(selector.select(remaining)), "stream deadline"
            )
            block = os.read(sys.stdin.fileno(), min(65536, limit + 1 - len(data)))
            if not block:
                return bytes(data)
            data.extend(block)
            require(len(data) <= limit, "stream limit")


@dataclass
class Signals:
    stopped: bool = False
    original: dict[int, signal._HANDLER] = field(default_factory=dict)

    def stop(self, _number: int, _frame: object) -> None:
        self.stopped = True

    def start(self) -> None:
        if sys.platform != "win32":
            for number, handler in (
                (signal.SIGHUP, signal.SIG_IGN),
                (signal.SIGPIPE, signal.SIG_IGN),
                (signal.SIGTERM, self.stop),
                (signal.SIGINT, self.stop),
            ):
                self.original[number] = signal.signal(number, handler)
        else:
            message = "receive requires Unix"
            raise RefusalError(message)

    def restore(self) -> None:
        for number, handler in self.original.items():
            signal.signal(number, handler)

    def check(self) -> None:
        if self.stopped:
            message = "stop signal"
            raise CommandError(message)


@dataclass
class Receiver:
    wanted: Request
    service: Service
    engine: Engine
    out: TextIO
    started: float = field(default_factory=time.monotonic)
    step: str = "request"
    incoming: Path | None = None
    created: bool = False
    signals: Signals = field(default_factory=Signals)

    def record(self, outcome: str, reason: str | None) -> str:
        record = {
            "stack": self.wanted.service,
            "release": self.wanted.release,
            "outcome": outcome,
            "step": self.step,
            "duration_s": round(time.monotonic() - self.started, 1),
            "run_id": self.wanted.run_id,
        }
        if reason is not None:
            record["reason"] = reason
        return json.dumps(record, sort_keys=True) + "\n"

    def output(self, body: str) -> None:
        try:
            self.out.write(body)
            self.out.flush()
        except BrokenPipeError, OSError:
            pass

    def line(self, outcome: str, reason: str | None = None) -> None:
        body = self.record(outcome, reason)
        with suppress(CommandError, OSError):
            self.engine.command(
                ["/usr/bin/systemd-cat", "--identifier=tbhb-deploy-receive"],
                body.encode(),
            )
        self.output(body)

    def progress(self, step: str) -> None:
        self.step = step
        self.line("progress")

    def prepare(self) -> None:
        info = fs.inspect(self.service.root)
        require(
            info is not None
            and stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) == ROOT_MODE,
            "service root ownership or mode",
        )
        for name in ("deploy", "incoming", "releases"):
            fs.mkdir(self.service.root / name)
        fs.mkdir(self.service.root / "deploy" / "models")
        if self.service.init:
            executable = fs.inspect(Path(self.service.init[0]), file=True)
            require(
                executable is not None and executable.st_uid == 0,
                "init executable owner",
            )

    def stage(self, release: Release) -> None:
        destination = self.service.root / "releases" / self.wanted.release
        retained = sum(
            path.stat().st_size
            for path in (self.service.root / "releases").rglob("*")
            if path.is_file()
        )
        added = (
            0
            if destination.exists()
            else sum(len(body) for body in release.files.values())
            + len(release.digest.encode())
        )
        require(
            retained + added <= self.service.max_retained_mib * 1024**2,
            "retained bytes limit",
        )
        self.incoming = Path(tempfile.mkdtemp(dir=self.service.root / "incoming"))
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

    def cleanup(self, *, before_secrets: bool) -> None:
        if self.incoming is not None and self.incoming.exists():
            shutil.rmtree(self.incoming)
        if before_secrets and self.created:
            shutil.rmtree(self.service.root / "releases" / self.wanted.release)
            fs.unlink(self.engine.model_path(self.wanted.release))

    def rollback(self, transaction: Transaction) -> tuple[int, str]:
        self.progress("rollback")
        journal = transaction.journal()
        require(journal is not None, "missing transaction")
        journal = mapping(journal)
        self.engine.stop_unit()
        self.engine.stop()
        transaction.restore(journal)
        current = journal["current"]
        if current is None:
            self.engine.remove()
            transaction.delete()
            return 3, "failed"
        self.engine.up(fs.release_name(text(current)), recreate=True)
        transaction.delete()
        return 1, "rolled_back"

    def pull(self, *, manual: bool, identical: bool) -> None:
        if not identical:
            self.progress("pull")
            if not manual or not self.engine.local_images(self.wanted.release):
                self.engine.run(self.wanted.release, "pull")

    def apply(
        self, transaction: Transaction, contents: dict[str, bytes], *, manual: bool
    ) -> tuple[int, str]:
        identical = not manual and transaction.identical(self.wanted.release, contents)
        self.progress("secrets")
        journal = transaction.save(self.wanted.release)
        # False and None select the same compensation path before switching.
        switched = (  # pragma: no mutate
            False
        )  # fmt: skip
        try:
            changed = (
                transaction.write_saved() if manual else transaction.write(contents)
            )
            if self.service.init and not manual and not identical:
                self.engine.command(list(self.service.init))
            self.signals.check()
            self.pull(manual=manual, identical=identical)
            self.signals.check()
            self.progress("switch")
            switched = True
            if journal["current"] != f"releases/{self.wanted.release}":
                transaction.pointer(
                    "previous", text(journal["current"]) if journal["current"] else None
                )
            transaction.pointer("current", f"releases/{self.wanted.release}")
            recreate = changed or (transaction.state / "force-recreate").exists()
            self.engine.check_paths(
                self.engine.stored(self.wanted.release), self.wanted.release
            )
            flags = ["up", "-d", "--wait", "--remove-orphans"]
            if recreate:
                flags.append("--force-recreate")
            self.engine.run(self.wanted.release, *flags)
            self.signals.check()
            self.progress("health")
            self.engine.health(self.wanted.release)
            self.signals.check()
        except RefusalError:
            # A unit stop refusal withholds compensation while a writer may be live.
            raise
        except Exception:  # noqa: BLE001  # Other apply failures need compensation.
            if switched:
                return self.rollback(transaction)
            self.engine.stop_unit()
            transaction.restore(journal)
            transaction.delete()
            return 4, "aborted"
        fs.unlink(transaction.state / "force-recreate")
        self.progress("done")
        transaction.finish(journal)
        return 0, "ok"

    def read_release(self) -> Release:
        self.step = "stream"
        return unpack(
            stream(self.service.max_stream_mib * 1024**2),
            self.wanted,
            {secret.name for secret in self.service.secrets},
            self.service.max_stream_mib * 1024**2,
        )

    def contents(
        self, *, manual: bool, release: Release | None = None
    ) -> dict[str, bytes]:
        if manual:
            previous = fs.target(self.service.root / "previous")
            require(
                previous is not None,
                "no previous release",
            )
            previous = text(previous)
            self.wanted = Request(self.wanted.service, fs.release_name(previous), None)
            # Manual apply reads write_saved(), so contents is never consumed.
            contents: dict[str, bytes] = {}  # pragma: no mutate
        else:
            self.progress("space")
            require(
                shutil.disk_usage(self.service.root).free
                >= self.service.min_free_gib * 1024**3,
                "free space",
            )
            if release is None:
                release = self.read_release()
            self.progress("release")
            self.stage(release)
            self.progress("policy")
            self.engine.parse(self.wanted.release)
            contents = release.secrets
        return contents

    def run(self, *, manual: bool = False) -> int:
        try:
            self.signals.start()
            try:
                release = None if manual else self.read_release()
                if release is not None:
                    self.step = "capabilities"
                    self.engine.preflight(self.wanted.release, release.files)
            except (CommandError, OSError, ValueError, KeyError, TypeError) as error:
                reason = (
                    str(error)
                    if isinstance(error, RefusalError)
                    else "receiver step failed"
                )
                self.output(self.record("refused", reason))
                return 2
            return self.deploy(release, manual=manual)
        finally:
            self.signals.restore()

    def recover(self, transaction: Transaction, *, manual: bool) -> bool:
        try:
            return transaction.recover()
        except RecoveryPendingError:
            if manual:
                raise
            self.engine.stop_unit()
            transaction.preserve()
            return False

    def deploy(self, release: Release | None, *, manual: bool) -> int:
        transaction = Transaction(self.service, self.engine)
        lock = Lock(self.service.root / "deploy")
        # False and None both mean that the secret phase has not started.
        secrets_started = (  # pragma: no mutate
            False
        )  # fmt: skip
        try:
            self.prepare()
            self.progress("lock")
            lock.acquire()
            self.engine.stop_unit()
            recovered = self.recover(transaction, manual=manual)
            if recovered:
                self.line("recovered")
                if manual:
                    self.line("ok")
                    return 0
            transaction.prune()
            contents = self.contents(manual=manual, release=release)
            secrets_started = True
            code, outcome = self.apply(transaction, contents, manual=manual)
            if code != 0:
                self.cleanup(before_secrets=True)
            self.line(outcome, None if code == 0 else "deployment compensated")
        except BusyError:
            self.line("busy", "live deploy lock")
            return 75
        except (
            CommandError,
            OSError,
            RefusalError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            code, outcome = (
                (3, "failed")
                if secrets_started or transaction.txn.exists()
                else (2, "refused")
            )
            reason = (
                str(error)
                if isinstance(error, RefusalError)
                else "receiver step failed"
            )
            self.line(outcome, "recovery failed" if manual else reason)
            return code
        else:
            return code
        finally:
            self.cleanup(before_secrets=not secrets_started)
            lock.release()


def identity(rollback: str | None) -> Request:
    require(
        effective_uid() == 0,
        "receive requires root",
    )
    if rollback is None:
        require(
            os.environ.get("SUDO_USER") == "deploy",
            "receive requires deploy sudo user",
        )
        wanted = request(os.environ.get("SSH_ORIGINAL_COMMAND", EMPTY_REQUEST))
    else:
        require(
            os.environ.get("SUDO_USER") != "deploy",
            "deploy cannot request rollback",
        )
        require(
            re.fullmatch(r"[a-z][a-z0-9-]{0,31}", rollback) is not None,
            "rollback service",
        )
        wanted = Request(rollback, "0" * 40, None)
    return wanted


def identity_fields(
    wanted: Request | None,
) -> tuple[str | None, str | None, str | None]:
    if wanted is None:
        return None, None, None
    return wanted.service, wanted.release, wanted.run_id


def receive(out: TextIO, *, rollback: str | None = None) -> int:
    require(sys.platform != "win32", "receive requires Unix")
    started = time.monotonic()
    # None and the empty-string mutant are both false before assignment.
    wanted: Request | None = None  # pragma: no mutate
    try:
        wanted = identity(rollback)
        allowed = service(
            load(CONFIG / "services" / f"{wanted.service}.toml"), wanted.service
        )
        receiver = mapping(
            load(CONFIG / "receiver.toml"), {"compose_binary", "compose_version"}
        )
        binary = text(receiver.get("compose_binary"))
        require(
            Path(binary).is_absolute(),
            "Compose binary path",
        )
        require(
            receiver.get("compose_version") == VERSION,
            "unsupported Compose version",
        )
        verify_binary(Path(binary))
        command = Command()
        require(
            command(
                [binary, "version", "--short"],
                timeout_s=10,
                stdout_limit=4096,
                stderr_limit=4096,
            )
            .decode()
            .strip()
            == VERSION,
            "Compose version mismatch",
        )
        return Receiver(
            wanted, allowed, Engine(allowed, binary, command), out, started
        ).run(manual=rollback is not None)
    except (
        RefusalError,
        OSError,
        ValueError,
        CommandError,
        KeyError,
        TypeError,
    ) as error:
        recovery_failed = rollback is not None and isinstance(
            error, (KeyError, TypeError)
        )
        reason = str(error) if isinstance(error, RefusalError) else "request refused"
        stack, release, run_id = identity_fields(wanted)
        record = {
            "stack": stack,
            "release": release,
            "outcome": "failed" if recovery_failed else "refused",
            "step": "recovery" if recovery_failed else "request",
            "duration_s": round(time.monotonic() - started, 1),
            "run_id": run_id,
            "reason": "recovery failed" if recovery_failed else reason,
        }
        out.write(json.dumps(record) + "\n")
        return 3 if recovery_failed else 2


def effective_uid() -> int:
    if sys.platform != "win32":
        return os.geteuid()
    return -1
