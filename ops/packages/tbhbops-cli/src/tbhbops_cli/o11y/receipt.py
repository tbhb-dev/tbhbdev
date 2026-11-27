"""Send and query a receipt through explicit system seams."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO
lazy import argparse
lazy from collections.abc import Callable, Mapping, Sequence
lazy from pathlib import Path

from tbhbops.o11y import receipt as model
from tbhbops.o11y import validate
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError

if TYPE_CHECKING:
    from tbhbops_cli.cli import System

REQUEST_TIMEOUT = 15
POLL_TIMEOUT = 60
POLL_INTERVAL = 2
GRPC_TIMEOUT = 120
# Tempo hides the newest 30 seconds from search.
GRPC_SEARCH_TIMEOUT = 120
HTTP_OK = 200


def _poll(
    system: System, check: Callable[[], bool], timeout: int = POLL_TIMEOUT
) -> bool:
    deadline = system.monotonic() + timeout
    while not check():
        if system.monotonic() >= deadline:
            return False
        system.sleep(POLL_INTERVAL)
    return True


def _nonzero(system: System, size: int) -> str:
    while True:
        data = system.random_bytes(size)
        if any(data):
            return data.hex()


class ReceiptError(Exception):
    """A safe receipt failure."""


@dataclass
class Check:
    system: System
    grafana: str
    run: str
    trace: str
    value: int
    sent: int
    rechecking: bool = False
    log_post: str = "no POST"
    log_detail: str = ""
    log_http: int = 0

    @property
    def service(self) -> str:
        return f"o11y-check-{self.run}"

    def request(
        self, method: str, url: str, body: bytes | None = None
    ) -> tuple[int, bytes, str]:
        try:
            status, data = self.system.receipt_http.request(
                method,
                url,
                body,
                {"Content-Type": "application/json"} if body is not None else {},
                REQUEST_TIMEOUT,
            )
        except BodyTooLargeError:
            # An oversized query is invalid JSON; an oversized POST cannot pass.
            return (HTTP_OK if method == "GET" else 0), b"", ""
        except TransportError as exc:
            return 0, b"", str(exc)
        return status, data, ""

    def query(self, kind: str) -> tuple[int, bytes, str]:
        return self.request(
            "GET",
            self.grafana
            + model.query_path(
                kind, self.run, self.trace, self.sent, int(self.system.epoch())
            ),
        )

    def post(self, otlp: str | None, wanted: list[str], span: str) -> None:
        for signal in ("traces", "logs", "metrics"):
            if signal in wanted:
                path = f"/v1/{signal}"
                status, body, _transport = self.request(
                    "POST",
                    f"{otlp}{path}",
                    model.payload(
                        signal, self.run, self.trace, span, self.value, self.sent
                    ),
                )
                if signal == "logs":
                    self.log_post = f"post_http={status:03d} post_bytes={len(body)}"
                if status != HTTP_OK:
                    msg = f"POST {path} returned {status:03d}"
                    raise ReceiptError(msg)

    def trace_check(self) -> bool:
        status, body, _transport = self.query("trace")
        return status == HTTP_OK and model.trace_found(body, self.service)

    def metric_check(self) -> bool:
        kind = "stored" if self.rechecking else "fresh"
        status, body, _transport = self.query(kind)
        if status != HTTP_OK or not model.metric_found(body, self.value, kind):
            return False
        if self.rechecking:
            return True
        status, body, _transport = self.query("timestamp")
        return status == HTTP_OK and model.metric_timestamp(
            body, int(self.system.epoch())
        )

    def log_check(self) -> bool:
        self.log_http, body, transport = self.query("log")
        self.log_detail = model.log_classify(
            self.log_http, body, self.run, self.floor, transport
        )
        return self.log_detail == "case=accepted count=1"

    @property
    def floor(self) -> int:
        return 0 if self.rechecking else self.sent - 60

    def log_receipt(self, lines: list[str]) -> bool:
        started = self.system.monotonic()
        if _poll(self.system, self.log_check):
            return True
        query_http = f"{self.log_http:03d}" if self.log_http else "unavailable"
        elapsed = int(self.system.monotonic() - started)
        lines.append(
            f"FAIL receipt Loki: {self.log_detail} query_http={query_http} "
            f"floor_s={self.floor} elapsed_s={elapsed} {self.log_post}"
        )
        return False

    def signal_check(self, signal: str, lines: list[str]) -> tuple[bool, str, str]:
        if signal == "traces":
            passed = _poll(self.system, self.trace_check)
            failure = (
                f"trace {self.trace} is no longer returned"
                if self.rechecking
                else f"trace {self.trace} was not returned by Tempo"
            )
            success = f"trace {self.trace} found in Tempo with service {self.service}"
        elif signal == "logs":
            passed = self.log_receipt(lines)
            failure = (
                f"the log line of run {self.run} is no longer returned"
                if self.rechecking
                else f"Loki did not return exactly the one log line of run {self.run}"
            )
            success = "exactly one log line with the run's body found in Loki"
        else:
            passed = _poll(self.system, self.metric_check)
            failure = (
                f"the metric sample of run {self.run} is no longer returned"
                if self.rechecking
                else f"Mimir did not return one fresh sample equal to "
                f"{self.value} for run {self.run}"
            )
            success = f"one fresh sample equal to {self.value} found in Mimir"
        return passed, failure, success

    def checks(self, wanted: list[str], lines: list[str]) -> None:
        for signal in ("traces", "logs", "metrics"):
            if signal not in wanted:
                continue
            passed, failure, success = self.signal_check(signal, lines)
            if not passed:
                raise ReceiptError(failure)
            if not self.rechecking:
                lines.append(f"ok receipt: {success}")

    def grpc_check(self) -> bool:
        status, body, _transport = self.query("grpc")
        return status == HTTP_OK and model.grpc_found(body)

    def grpc(
        self, argv: Sequence[str], env: Mapping[str, str], lines: list[str]
    ) -> None:
        service = f"o11y-check-grpc-{self.run}"
        result = self.system.runner.run(
            [*argv, "--traces", "1", "--service", service],
            env,
            GRPC_TIMEOUT,
        )
        if result.code != 0:
            msg = "telemetrygen could not send a trace over gRPC"
            raise ReceiptError(msg)
        if not _poll(self.system, self.grpc_check, GRPC_SEARCH_TIMEOUT):
            msg = f"no trace for service {service} was found"
            raise ReceiptError(msg)
        lines.append(f"ok receipt: gRPC trace found in Tempo by service {service}")


def _arguments(  # receipt argument validation
    otlp: str | None,
    signals: str,
    state: Path | None,
    recheck: Path | None,
    grpc: Sequence[str] | None,
) -> None:
    if not re.fullmatch(r"(traces|logs|metrics)(,(traces|logs|metrics))*", signals):
        msg = "invalid signals"
        raise ValueError(msg)
    if state is not None and recheck is not None:
        msg = "state-file and recheck are mutually exclusive"
        raise ValueError(msg)
    if recheck is not None and grpc is not None:
        msg = "grpc and recheck are mutually exclusive"
        raise ValueError(msg)
    if recheck is None and otlp is None:
        msg = "otlp-url is required"
        raise ValueError(msg)
    if grpc is not None and (not grpc or "traces" not in signals.split(",")):
        msg = "grpc requires an argv and traces"
        raise ValueError(msg)


def _signal_failure(
    lines: list[str], exc: ReceiptError, *, safe_failures: bool
) -> tuple[list[str], bool]:
    lines.append(
        "FAIL receipt: signal-check" if safe_failures else f"FAIL receipt: {exc}"
    )
    if safe_failures:
        lines = [line for line in lines if not line.startswith("FAIL receipt Loki:")]
    return lines, False


def receipt(  # noqa: PLR0913, PLR0917  # shared in-process smoke API
    system: System,
    otlp_url: str | None,
    grafana_url: str,
    signals: str = "traces,logs,metrics",
    state_file: Path | None = None,
    recheck: Path | None = None,
    grpc_argv: Sequence[str] | None = None,
    docker_env: Mapping[str, str] | None = None,
    *,
    safe_failures: bool = False,
) -> tuple[list[str], bool]:
    """Run a receipt in process. Invalid arguments raise ValueError.

    The caller passes an already-filtered Docker environment in docker_env.
    """
    grafana = model.base_url(grafana_url)
    otlp = model.base_url(otlp_url) if otlp_url is not None else None
    _arguments(otlp, signals, state_file, recheck, grpc_argv)
    lines: list[str] = []
    try:
        if recheck is not None:
            run, trace, value, sent = model.parse_state(
                system.filesystem.read_text(recheck)
            )
            check = Check(system, grafana, run, trace, value, sent, rechecking=True)
        else:
            run = system.random_bytes(8).hex()
            trace = _nonzero(system, 16)
            span = _nonzero(system, 8)
            value = int.from_bytes(system.random_bytes(4)) & ((1 << 30) - 1)
            sent = int(system.epoch())
            check = Check(system, grafana, run, trace, value, sent)
            check.post(otlp, signals.split(","), span)
        wanted = signals.split(",")
        check.checks(wanted, lines)
        if recheck is not None:
            lines.append(
                f"ok receipt: run {run} still returns its {signals.replace(',', ', ')}"
            )
        else:
            if grpc_argv is not None:
                check.grpc(grpc_argv, docker_env or {}, lines)
            if state_file is not None:
                system.filesystem.write_atomic(
                    state_file, model.state_line(run, trace, value, sent), 0o600
                )
    except ReceiptError as exc:
        return _signal_failure(lines, exc, safe_failures=safe_failures)
    except (OSError, ValueError) as exc:
        lines.append(
            "FAIL receipt: operation-unavailable"
            if safe_failures
            else f"FAIL receipt: {type(exc).__name__}"
        )
        return lines, False
    return lines, True


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    argv = args.grpc_argv
    if bool(argv) != args.grpc or (argv and (argv[0] != "--" or len(argv) == 1)):
        out.write("tbhbops o11y receipt: grpc requires -- ARGV\n")
        return 2
    if argv:
        argv = argv[1:]
    try:
        lines, ok = receipt(
            system,
            args.otlp_url,
            args.grafana_url,
            args.signals,
            args.state_file,
            args.recheck,
            argv if args.grpc else None,
            validate.docker_environment(system.env),
        )
    except ValueError as exc:
        out.write(f"tbhbops o11y receipt: {exc}\n")
        return 2
    out.writelines(f"{line}\n" for line in lines)
    return 0 if ok else 1
