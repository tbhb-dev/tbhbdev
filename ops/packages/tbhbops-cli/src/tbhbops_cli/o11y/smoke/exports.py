"""Verify receipts and telemetry persistence across backend restarts."""

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from tbhbops.o11y import smoke_export as export
from tbhbops.o11y import smoke_filter as spans

from tbhbops_cli.o11y import receipt
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke.safety import SmokeError, present, require

if TYPE_CHECKING:
    from tbhbops_cli.o11y.smoke.runtime import Smoke


from tbhbops_cli.o11y.smoke import readiness
from tbhbops_cli.o11y.smoke.grafana import get
from tbhbops_cli.o11y.smoke.transport import (
    GRAFANA,
    HTTP_OK,
    OTLP,
    REQUEST_TIMEOUT,
    request,
)

UPLOAD_CURL_TIMEOUT = 15
FILTER_TIMEOUT = 60
QUERY_TIMEOUT = 30
BUCKET_TIMEOUT = 120
TEMPO_BUCKET_TIMEOUT = 240


def codex_span_filter(run: Smoke) -> None:
    control = "o11y-check-" + run.system.random_bytes(8).hex()
    sent = int(run.system.epoch())
    for service in ("codex_exec", "codex_tui", control):
        trace = run.system.random_bytes(16).hex()
        fixture = spans.fixture(service, trace, sent)
        try:
            code, _ = run.system.receipt_http.request(
                "POST",
                OTLP + "/v1/traces",
                fixture,
                {"Content-Type": "application/json"},
                REQUEST_TIMEOUT,
            )
        except BodyTooLargeError, TransportError:
            rule = "5: codex-filter-post"
            raise SmokeError(rule) from None
        require(code == HTTP_OK, "5: codex-filter-post")

        def found(
            trace: str = trace, service: str = service, fixture: bytes = fixture
        ) -> bool:
            code, body = request(
                run, "/api/datasources/proxy/uid/tempo/api/traces/" + trace
            )
            return code == HTTP_OK and spans.found(body, service, fixture)

        require(poll(run.system, FILTER_TIMEOUT, found), "5: codex-filter-spans")
    run.emit("ok Codex filter: exact span names, IDs, parents and attributes")


def group5(run: Smoke) -> None:
    run.record_loki_disk("before-group-5")
    readiness.loki_write_probe(run)
    image = present(
        run.model.get("telemetrygen", {}).get("image"), "5: telemetrygen-image"
    )
    network = ["--network", "host"] if run.linux else []
    desktop_domain = "docker.internal"
    endpoint = "127.0.0.1:14317" if run.linux else f"host.{desktop_domain}:14317"
    argv = [
        "docker",
        "run",
        "--rm",
        *network,
        image,
        "traces",
        "--otlp-endpoint",
        endpoint,
        "--otlp-insecure",
    ]
    lines, passed = receipt.receipt(
        run.system,
        OTLP,
        GRAFANA,
        state_file=run.out / "receipt",
        grpc_argv=argv,
        docker_env=run.env,
        safe_failures=True,
    )
    for line in lines:
        run.emit(line)
    require(passed, "5: receipt")
    run.emit("ok 5: receipt over OTLP/HTTP and gRPC through the published ports")
    codex_span_filter(run)


def history_query(run: Smoke, query: str) -> bytes:
    url = GRAFANA + "/api/datasources/proxy/uid/mimir/api/v1/query_range?" + query
    try:
        code, body = run.system.receipt_http.request(
            "GET", url, None, {}, QUERY_TIMEOUT
        )
    except BodyTooLargeError, TransportError:
        rule = "export persistence: query-http"
        raise SmokeError(rule) from None
    require(code == HTTP_OK, "export persistence: query-http")
    return body


def export_persistence(run: Smoke) -> None:
    started = run.system.monotonic()
    end = int(run.system.epoch()) - 1800
    rule = present(
        export.production_rule(
            get(run, "/api/v1/provisioning/alert-rules/o11y-alloy-export")
        ),
        "export persistence: production-rule",
    )
    expr, pending = rule
    path = run.out / "export-fixtures.json"
    fixture = export.histories(end)
    run.fs.write_atomic(path, fixture, 0o600)
    # Feed stdin so the container user need not read a host-owned private file.
    result = run.inside(
        "http://mimir:8080/otlp/v1/metrics",
        body=True,
        args=(
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
        ),
        input_data=fixture.decode(),
        curl_timeout=UPLOAD_CURL_TIMEOUT,
    )
    require(result.code == 0, "export persistence: upload-exec")
    body, separator, code = result.stdout.rstrip("\n").rpartition("\n")
    require(
        bool(separator) and code == "200",
        "export persistence: upload-http",
    )
    require(export.upload(body.encode()), "export persistence: rejected-points")
    query = urlencode(
        {"query": 'count({job="o11y/alloy",component_id=~"smoke-.*"})', "time": end}
    )
    require(
        export.arrived(
            get(run, "/api/datasources/proxy/uid/mimir/api/v1/query?" + query)
        ),
        "export persistence: fixture-count",
    )
    query = urlencode(
        {"query": expr, "start": end - pending, "end": end, "step": export.STEP}
    )
    require(
        export.sustained(history_query(run, query), pending),
        "export persistence: sustained-loss",
    )
    elapsed = int(run.system.monotonic() - started)
    run.emit(
        "ok export persistence: bursts, resets and healed loss stay quiet; "
        f"sustained loss spans the pending period ({elapsed} seconds)"
    )


def recheck(run: Smoke) -> None:
    lines, passed = receipt.receipt(
        run.system, None, GRAFANA, recheck=run.out / "receipt", safe_failures=True
    )
    for line in lines:
        run.emit(line)
    require(passed, "6: receipt-recheck")


def group6(run: Smoke) -> None:
    for url in ("http://loki:3100/flush", "http://mimir:8080/ingester/flush"):
        result = run.inside(url, args=("-X", "POST"))
        require(
            result.code == 0 and result.stdout.rstrip("\n") == "204", "6: flush-status"
        )
    for bucket, timeout in (
        ("loki", BUCKET_TIMEOUT),
        ("mimir-blocks", BUCKET_TIMEOUT),
        ("tempo", TEMPO_BUCKET_TIMEOUT),
    ):

        def has_objects(bucket: str = bucket) -> bool:
            result = run.probe(["count", bucket])
            return result.code == 0 and export.objects(result.stdout)

        require(poll(run.system, timeout, has_objects), "6: bucket-empty")
    require(run.compose(["rm", "-sf", "loki"]).code == 0, "6: loki-remove")
    # find includes dotfiles and preserves the owned mount root.
    # No glob or command interpreter.
    require(
        run.utility(
            ["find", run.destination + "/loki", "-mindepth", "1", "-delete"]
        ).code
        == 0,
        "6: loki-data-clear",
    )
    require(run.logged(["up", "-d", "loki"], "up.log").code == 0, "6: loki-create")
    readiness.wait_ready(run, "loki", 6)
    recheck(run)
    for service in ("tempo", "mimir"):
        require(run.compose(["restart", service]).code == 0, "6: backend-restart")
        readiness.wait_ready(run, service, 6)
        recheck(run)
    run.emit(
        "ok 6: buckets hold objects; Loki returns the receipt from its bucket alone; "
        "Tempo and Mimir keep it across restart"
    )
