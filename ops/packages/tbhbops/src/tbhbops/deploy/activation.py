"""Switch the live release and compensate for interrupted activation."""

from collections.abc import Callable
from dataclasses import dataclass

from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.config import text
from tbhbops.deploy.protocol import RefusalError, mapping, require
from tbhbops.deploy.receiver_io import Signals
from tbhbops.deploy.transaction import Transaction


@dataclass
class Activation:
    transaction: Transaction
    release: str
    signals: Signals
    progress: Callable[[str], None]

    def rollback(self) -> tuple[int, str]:
        transaction = self.transaction
        engine = transaction.engine
        self.progress("rollback")
        journal = transaction.journal()
        require(journal is not None, "missing transaction")
        journal = mapping(journal)
        engine.stop_unit()
        engine.stop()
        transaction.restore(journal)
        current = journal["current"]
        if current is None:
            engine.remove()
            transaction.delete()
            return 3, "failed"
        engine.up(fs.release_name(text(current)), recreate=True)
        transaction.delete()
        return 1, "rolled_back"

    def apply(self, contents: dict[str, bytes], *, manual: bool) -> tuple[int, str]:
        transaction = self.transaction
        engine = transaction.engine
        service = transaction.service
        identical = not manual and transaction.identical(self.release, contents)
        self.progress("secrets")
        journal = transaction.save(self.release)
        # False and None select the same compensation path before switching.
        switched = (  # pragma: no mutate
            False
        )  # fmt: skip
        try:
            changed = (
                transaction.write_saved() if manual else transaction.write(contents)
            )
            if service.init and not manual and not identical:
                engine.command(list(service.init))
            self.signals.check()
            if not identical:
                self.progress("pull")
                if not manual or not engine.local_images(self.release):
                    engine.run(self.release, "pull")
            self.signals.check()
            self.progress("switch")
            switched = True
            if journal["current"] != f"releases/{self.release}":
                transaction.pointer(
                    "previous", text(journal["current"]) if journal["current"] else None
                )
            transaction.pointer("current", f"releases/{self.release}")
            recreate = changed or (transaction.state / "force-recreate").exists()
            engine.check_paths(engine.stored(self.release), self.release)
            engine.start(self.release, recreate=recreate)
            self.signals.check()
            self.progress("health")
            engine.health(self.release)
            self.signals.check()
        except RefusalError:
            # A unit stop refusal withholds compensation while a writer may be live.
            raise
        except Exception:  # noqa: BLE001  # Other apply failures need compensation.
            if switched:
                return self.rollback()
            engine.stop_unit()
            transaction.restore(journal)
            transaction.delete()
            return 4, "aborted"
        fs.unlink(transaction.state / "force-recreate")
        self.progress("done")
        transaction.finish(journal)
        return 0, "ok"
