# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""One bounded, best-effort trace for a synchronous CLI command."""

from __future__ import annotations

import functools
import ipaddress
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, override
from urllib.parse import urlsplit

from tbhb_telemetry import exporter

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Span


_state: _Pending | None = None
_installed = False
_run_id = None
_KEYS = {
    "tbhb.agent." + key for key in ("host", "harness", "model", "profile", "outcome")
}
_KEYS.add("tbhb.run.id")


def _quiet(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:  # noqa: BLE001 -- Telemetry is best effort.
            return None

    return wrapped


def _drop_endpoint(url):
    host = url.hostname if url else None
    if not host:
        return None
    try:
        port = str(exporter.endpoint_port(url))
    except ValueError:
        return None
    host_limit = 128 - len(port) - 1
    if ":" in host:
        return f"[{host[: host_limit - 2]}]:{port}"
    return f"{host[:host_limit]}:{port}"


@_quiet
def _counter(delta=0, reason=None, url=None):
    import fcntl

    directory = (
        Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local/state")
        / "tbhb/telemetry"
    )
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "drops.lock").open("a") as lock:
        deadline = time.monotonic() + 0.02
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(0.002, remaining))
        file = directory / "drops.count"
        try:
            count = int(file.read_text())
        except (FileNotFoundError, ValueError):
            count = 0
        if reason:
            with (directory / "drops.jsonl").open("a") as log:
                if os.stat(log.fileno()).st_size < 1024 * 1024:
                    log.write(
                        json.dumps(
                            {
                                "timestamp": time.time(),
                                "count": delta,
                                "reason": reason,
                                "run_id": _run_id,
                                "endpoint": _drop_endpoint(url),
                            }
                        )
                        + "\n"
                    )
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False
        ) as temporary:
            path = Path(temporary.name)
            try:
                temporary.write(str(max(0, count + delta)))
                temporary.close()
                os.replace(path, file)
            finally:
                path.unlink(missing_ok=True)
        return count


def _endpoint_allowed(host):
    try:
        allowed = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    raw_hosts = os.getenv("TBHB_TELEMETRY_ALLOW_HOSTS", "")
    hosts = [
        entry.strip().lower()
        for entry in (raw_hosts if len(raw_hosts) <= 4096 else "").split(",")
    ]
    hosts = [entry for entry in hosts if 0 < len(entry) <= 253][:8]
    return allowed or host in hosts


class _Pending:
    def __init__(self, name, version):
        self.name, self.start = name, time.time_ns()
        self.attributes = {
            "process.executable.name": name,
            "service.name": name,
            "service.version": version,
            "tbhb.cli.command": name,
            "tbhb.export.dropped": _counter() or 0,
        }
        self.inbound = {
            key: os.getenv(key.upper(), "") for key in ("traceparent", "tracestate")
        }
        self.root: Span | None = None
        self.spans: list[ReadableSpan] = []
        self.lost = 0

    def _ensure_root(self) -> Span:
        if self.root is None:
            from opentelemetry import context, trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
            from opentelemetry.sdk.trace.sampling import ALWAYS_ON
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            pending = self

            class Processor(SpanProcessor):
                @override
                def on_end(self, span: ReadableSpan) -> None:
                    pending._record_span(span)

            provider = TracerProvider(
                resource=Resource(
                    {
                        "service.name": self.attributes["service.name"],
                        "service.version": self.attributes["service.version"],
                    }
                ),
                sampler=ALWAYS_ON,
                shutdown_on_exit=False,
            )
            provider.add_span_processor(Processor())
            self.tracer = provider.get_tracer("tbhb_telemetry")
            self.root = self.tracer.start_span(
                self.name,
                attributes=self.attributes,
                context=TraceContextTextMapPropagator().extract(self.inbound),
                start_time=self.start,
                record_exception=False,
                set_status_on_exception=False,
            )
            self.token = context.attach(trace.set_span_in_context(self.root))
        return self.root

    def materialize(self):
        return self._ensure_root().get_span_context()

    def _record_span(self, span: ReadableSpan) -> None:
        if len(self.spans) < 511 or (
            self.root is not None and span.context == self.root.get_span_context()
        ):
            self.spans.append(span)
        else:
            self.lost += 1

    def finish(self, code, error):
        from opentelemetry import context, trace

        root = self._ensure_root()
        root.set_attribute("process.exit.code", code)
        if error or code:
            root.set_attribute(
                "error.type", type(error).__name__ if error else "SystemExit"
            )
            root.set_status(trace.StatusCode.ERROR)
        root.end()
        context.detach(self.token)
        endpoint = os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            f"http://{ipaddress.ip_address(0x7F000001)}:4318",
        )
        try:
            url = urlsplit(endpoint)
            host = url.hostname
        except ValueError:
            url, host = None, None
        if self.lost:
            _counter(self.lost, "capacity", url)
        if not _endpoint_allowed(host):
            _counter(len(self.spans), "endpoint", url)
            return
        done, result = threading.Event(), []

        def send():
            try:
                exporter.export(self.spans, endpoint)
                result.append(True)
            except Exception:  # noqa: BLE001 -- Telemetry is best effort.
                result.append(False)
            finally:
                done.set()

        threading.Thread(target=send, daemon=True).start()
        try:
            completed = done.wait(0.25)
        except KeyboardInterrupt:
            return
        if not completed:
            _counter(len(self.spans), "timeout", url)
        elif not result[0]:
            _counter(len(self.spans), "export", url)
        else:
            _counter(-self.attributes["tbhb.export.dropped"])


@_quiet
def _setup(name, version):
    global _state, _installed
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    from opentelemetry import trace

    if not _installed:
        from opentelemetry.util._decorator import _agnosticcontextmanager

        class Tracer(trace.Tracer):
            def start_span(self, *args, **kwargs):
                try:
                    if _state:
                        _state.materialize()
                        kwargs.update(
                            record_exception=False, set_status_on_exception=False
                        )
                        return _state.tracer.start_span(*args, **kwargs)
                except Exception:  # noqa: BLE001 -- Telemetry is best effort.
                    return trace.INVALID_SPAN
                return trace.INVALID_SPAN

            @_agnosticcontextmanager
            def start_as_current_span(self, *args, **kwargs):
                end = kwargs.pop("end_on_exit", True)
                with trace.use_span(
                    self.start_span(*args, **kwargs),
                    end_on_exit=end,
                    record_exception=False,
                    set_status_on_exception=False,
                ) as span:
                    yield span

        class Provider(trace.TracerProvider):
            def get_tracer(self, *args, **kwargs):
                return Tracer()

        if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            trace.set_tracer_provider(Provider())
        _installed = True
    _state = _Pending(name, version)


@_quiet
def resolve(name):
    """Append a fixed command key or parsed subcommand name, never an argument value."""
    if _state:
        _state.name += " " + name
        _state.attributes["tbhb.cli.command"] = _state.name
        if _state.root:
            _state.root.update_name(_state.name)
            _state.root.set_attribute("tbhb.cli.command", _state.name)


@_quiet
def materialize():
    if _state:
        return _state.materialize()


@_quiet
def set_attributes(**kwargs):
    global _run_id
    run_id = kwargs.get("tbhb.run.id")
    if isinstance(run_id, str) and len(run_id) <= 128:
        _run_id = run_id
    if not _state:
        return
    for key, value in kwargs.items():
        if key not in _KEYS or not isinstance(value, str) or len(value) > 128:
            continue
        if key == "tbhb.agent.outcome" and value not in ("ok", "refused", "failed"):
            continue
        _state.attributes[key] = value
        if _state.root:
            _state.root.set_attribute(key, value)


def command(name, *, version, main):
    """Run main, preserving its result, output and exceptions."""
    global _state, _run_id
    _setup(name, version)
    code, error = 0, None
    try:
        code = main()
        return 0 if code is None else code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        error = exc if code else None
        raise
    except BaseException as exc:
        code, error = 1, exc
        raise
    finally:
        try:
            if _state:
                _quiet(_state.finish)(0 if code is None else code, error)
        finally:
            _state = None
            _run_id = None
