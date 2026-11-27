"""Scan and publish smoke evidence and clean up the stack."""

from typing import TYPE_CHECKING

from tbhbops.o11y import smoke

from tbhbops_cli.o11y import render
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke.safety import (
    SmokeError,
    require,
    scan_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

    from tbhbops_cli.o11y.smoke.runtime import Smoke

FIXTURES = ("readiness-unhealthy", "readiness-failed", "readiness-consumer")


def scans(run: Smoke) -> None:
    needles = run.sentinels.scan_values()
    paths = {run.out / name for name in ("transcript.log", "release", "config")}
    for name in run.fs.list_dir(run.out):
        if name.endswith(".log"):
            path = run.out / name
            if run.fs.read_bytes(path):
                paths.add(path)
    for path in sorted(paths):
        scan_path(run.fs, path, needles)
    if run.fs.lstat(run.artifacts) is not None and run.fs.list_dir(run.artifacts):
        scan_path(run.fs, run.artifacts, needles)


def capture_failure_logs(run: Smoke) -> bytes:
    stage = run.out / "failure-logs"
    run.fs.mkdir(stage)
    run.fs.chmod(stage, 0o700)
    logs = {}
    errors = bytes()  # noqa: UP018  # an initially empty error buffer
    for name, args in (
        ("alloy", ["logs", "--no-color", "--tail", "200", "alloy"]),
        ("loki", ["logs", "--no-color", "--tail", "200", "loki"]),
        ("loki-head", ["logs", "--no-color", "loki"]),
    ):
        result = run.compose(args, head=name == "loki-head")
        require(
            result.code == 0,
            smoke.FAILURES["diagnostics_capture_failed_no_logs_promoted"],
        )
        errors += result.stderr.encode(errors="surrogateescape")
        content = result.stdout.encode(errors="surrogateescape")
        logs[name] = b"".join(content.splitlines(keepends=True)[-200:])
        run.fs.write_atomic(stage / (name + ".log"), logs[name], 0o600)
    # Finalize synchronously before any search or publication.
    run.finalize_transcript()
    for name in logs:
        scan_path(run.fs, stage / (name + ".log"), run.sentinels.scan_values())
    if errors:
        run.fs.write_atomic(stage / "capture.err", errors, 0o600)
        scan_path(run.fs, stage / "capture.err", run.sentinels.scan_values())
    scans(run)
    # prepare created this directory; scans above also checks any evidence
    # already retained by a failed stage, such as SMTP delivery.
    run.fs.chmod(run.artifacts, 0o700)
    for name in logs:
        run.fs.rename(stage / (name + ".log"), run.artifacts / (name + ".log"))
    run.emit(smoke.MESSAGES["smoke_diagnostics_clean_alloy_and_loki_logs_retained"])
    return logs["loki-head"] + logs["loki"]


def cleanup(run: Smoke) -> bool:
    passed = True

    def attempt(call: Callable[[], object]) -> None:
        nonlocal passed
        try:
            result = call()
            if isinstance(result, Result) and result.code != 0:
                passed = False
        except BaseException:  # noqa: BLE001  # attempt every remaining cleanup even after a signal
            passed = False

    if run.image:
        attempt(
            lambda: run.compose(
                ["down", "-v", "--remove-orphans"], tools=True, dummy=True
            )
        )
        for name in FIXTURES:
            attempt(lambda name=name: remove_fixture(run, name))
        attempt(lambda: remove_data(run))
    attempt(lambda: render.remove_tree(run.fs, run.host))
    if run.output_ready:
        attempt(lambda: render.remove_tree(run.fs, run.out))
    return passed


def remove_data(run: Smoke) -> Result:
    # A bind mount root cannot itself be removed. Remove its children,
    # then let the host remove the empty private directory.
    paths = (
        [run.destination + "/" + name for name in run.fs.list_dir(run.data)]
        if run.linux
        else [run.destination]
    )
    return run.utility(["rm", "-rf", *paths])


def remove_fixture(run: Smoke, name: str) -> None:
    # down --remove-orphans already removes project fixtures. A missing name
    # is expected here; down's status remains the required engine evidence.
    run.run(["docker", "rm", "-f", run.project + "-" + name])


def finish(run: Smoke, status: int, out: TextIO) -> None:
    if status != 0 and run.started:
        logs = bytes()  # noqa: UP018  # an empty buffer, with no meaningful literal to mutate
        try:
            logs = capture_failure_logs(run)
        except SmokeError as exc:
            run.emit(f"smoke diagnostics: {exc}; no logs promoted")
        except BaseException:  # noqa: BLE001  # capture must preserve status and reach cleanup
            run.emit(
                smoke.MESSAGES[
                    "smoke_diagnostics_capture_or_scan_failed_no_logs_promoted"
                ]
            )
        try:
            for line in smoke.disk_evidence(run.disk_counts(), logs):
                run.emit(line)
        except BaseException:  # noqa: BLE001  # evidence must preserve status and reach cleanup
            run.emit(smoke.MESSAGES["smoke_diagnostics_disk_evidence_unavailable"])
    elif status != 0:
        run.emit(
            smoke.MESSAGES["smoke_diagnostics_capture_unavailable_stack_not_started"]
        )
    try:
        if run.output_ready:
            run.finalize_transcript()
    except BaseException:  # noqa: BLE001  # finalization must preserve status and reach cleanup
        run.emit(smoke.MESSAGES["smoke_diagnostics_transcript_finalization_failed"])
    if not cleanup(run):
        run.emit(smoke.MESSAGES["smoke_diagnostics_cleanup_failed"])
    out.write("".join(run.transcript))
