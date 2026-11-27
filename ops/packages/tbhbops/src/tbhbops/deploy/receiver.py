"""Run preflight, staging, and recovery for an authorized deployment."""

import json
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TextIO

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import receiver_io, staging
from tbhbops.deploy.activation import Activation
from tbhbops.deploy.command import CommandError
from tbhbops.deploy.compose import Engine
from tbhbops.deploy.config import Service, text
from tbhbops.deploy.protocol import RefusalError, Release, Request, require, unpack
from tbhbops.deploy.receiver_io import Signals
from tbhbops.deploy.staging import Staging
from tbhbops.deploy.transaction import (
    BusyError,
    Lock,
    RecoveryPendingError,
    Transaction,
)


@dataclass
class Receiver:
    wanted: Request
    service: Service
    engine: Engine
    out: TextIO
    started: float = field(default_factory=time.monotonic)
    step: str = "request"
    staging: Staging = field(default_factory=Staging)
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

    def cleanup(self, *, before_secrets: bool) -> None:
        self.staging.cleanup(
            self.service.root / "releases" / self.wanted.release,
            self.engine.model_path(self.wanted.release),
            before_secrets=before_secrets,
        )

    def read_release(self) -> Release:
        self.step = "stream"
        return unpack(
            receiver_io.stream(self.service.max_stream_mib * 1024**2),
            self.wanted,
            {secret.name for secret in self.service.secrets},
            self.service.max_stream_mib * 1024**2,
        )

    def contents(self, release: Release | None) -> dict[str, bytes]:
        if release is None:
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
            self.progress("release")
            self.staging.publish(self.service, self.wanted.release, release)
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
            staging.prepare(self.service)
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
            contents = self.contents(release)
            secrets_started = True
            activation = Activation(
                transaction, self.wanted.release, self.signals, self.progress
            )
            code, outcome = activation.apply(contents, manual=manual)
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
