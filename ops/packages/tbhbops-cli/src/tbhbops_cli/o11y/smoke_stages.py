"""Grafana access, receipts, export histories and persistence smoke stages."""

import json
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from tbhbops.o11y import init
from tbhbops.o11y import smoke_access as access
from tbhbops.o11y import smoke_export as export
from tbhbops.o11y import smoke_filter as spans
from tbhbops_cli.o11y import receipt
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.smoke import SmokeError, poll, present, require

if TYPE_CHECKING:
    from tbhbops_cli.o11y.smoke import Smoke

GRAFANA = "http://127.0.0.1:13000"
OTLP = "http://127.0.0.1:14318"
HTTP_OK = 200
REQUEST_TIMEOUT = 15
UPLOAD_CURL_TIMEOUT = 15
FILTER_TIMEOUT = 60
QUERY_TIMEOUT = 30
BUCKET_TIMEOUT = 120
TEMPO_BUCKET_TIMEOUT = 240


def request(
    run: Smoke,
    path: str,
    *,
    login: str = "",
    method: str = "GET",
    data: bytes | None = None,
) -> tuple[int, bytes]:
    headers = {"Tailscale-User-Login": login} if login else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        return run.system.receipt_http.request(
            method, GRAFANA + path, data, headers, REQUEST_TIMEOUT
        )
    except BodyTooLargeError, TransportError:
        return 0, b""


def get(run: Smoke, path: str, *, login: str = "") -> bytes:
    status, body = request(run, path, login=login)
    require(status == HTTP_OK, "4: grafana-query-unavailable")
    return body


def status(
    run: Smoke,
    path: str,
    expected: int,
    *,
    login: str = "",
    operation: tuple[str, bytes | None] = ("GET", None),
) -> None:
    method, data = operation
    actual, _ = request(run, path, login=login, method=method, data=data)
    require(actual == expected, "4: grafana-permission-status")


def proxy_role(run: Smoke, login: str, role: str, *, recovery: bool = False) -> None:
    require(
        access.user(get(run, "/api/user", login=login), login, admin=recovery),
        "4: proxy-user",
    )
    require(
        access.orgs(get(run, "/api/user/orgs", login=login), role, recovery=recovery),
        "4: proxy-org-role",
    )


def admin_login(run: Smoke) -> None:
    path = run.destination + "/secrets/grafana_admin_password"
    if run.linux:
        require(
            run.utility(["chown", f"{run.system.euid}:{run.system.egid}", path]).code
            == 0,
            "4: recovery-file-owner",
        )
    try:
        password = run.fs.read_bytes(run.host / "secrets/grafana_admin_password")
    finally:
        if run.linux:
            require(
                run.utility(["chown", "472:472", path]).code == 0,
                "4: recovery-file-restore",
            )
    for value, expected in ((password, 200), (b"admin", 403)):
        try:
            actual, _ = run.system.admin_http.request(
                GRAFANA + "/api/admin/settings", value, REQUEST_TIMEOUT, method="GET"
            )
        except BodyTooLargeError, TransportError:
            rule = "4: recovery-login-status"
            raise SmokeError(rule) from None
        require(actual == expected, "4: recovery-login-status")


def dashboard_checkpoint(run: Smoke) -> None:
    paths = ["/api/search?type=dash-db", f"/api/folders/{access.FOLDER}"]
    paths.extend(
        f"/api/dashboards/uid/{uid}"
        for uid in (*access.AGENTS, "o11y-github-activity", access.HEALTH)
    )
    require(
        access.dashboard_checkpoint({path: get(run, path) for path in paths}),
        "4: dashboard-checkpoint",
    )


def group4(run: Smoke) -> None:
    status(run, "/api/org", 200)
    proxy_role(run, access.OWNER, "Admin")
    proxy_role(run, access.VIEWER, "Editor")
    proxy_role(run, "admin", "Admin", recovery=True)
    require(access.owner_model(run.model), "4: owner-service-model")
    owner = run.inspect(
        "grafana-owner", "{{.State.Status}} {{.State.ExitCode}}", all_containers=True
    )
    require(
        owner.code == 0 and owner.stdout.strip() == "exited 0", "4: owner-service-exit"
    )
    whitelist = run.compose(
        [
            "exec",
            "-T",
            "grafana",
            "sh",
            "-c",
            (
                'tr "\\000" "\\n" </proc/1/environ | '
                'sed -n "s/^GF_AUTH_PROXY_WHITELIST=//p"'
            ),
        ]
    )
    require(
        whitelist.code == 0 and access.whitelist(whitelist.stdout), "4: proxy-whitelist"
    )
    header = ("-H", f"Tailscale-User-Login: {access.OWNER}")
    for path, expected, args in (
        ("/api/user", 401, header),
        ("/api/org", 200, header),
        ("/api/org", 200, ()),
        (
            "/api/dashboards/db",
            403,
            (
                *header,
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-d",
                '{"dashboard":{"uid":"o11y-forged","title":"forged"}}',
            ),
        ),
    ):
        result = run.inside("http://grafana:3000" + path, args=args)
        require(
            result.code == 0 and result.stdout.rstrip("\n") == str(expected),
            "4: peer-proxy-permission",
        )
    run.emit(
        "ok AC5: host owner Admin and fresh login Editor; "
        "peer requests remain anonymous Viewer"
    )
    save = b'{"dashboard":{"uid":"o11y-smoke","title":"o11y smoke"},"overwrite":true}'
    for login, expected in (("", 403), (access.OWNER, 200)):
        status(
            run, "/api/dashboards/db", expected, login=login, operation=("POST", save)
        )
    for login, expected in (("", 403), (access.OWNER, 200)):
        status(
            run,
            "/api/dashboards/uid/o11y-smoke",
            expected,
            login=login,
            operation=("DELETE", None),
        )
    status(
        run,
        "/api/datasources",
        403,
        operation=(
            "POST",
            b'{"name":"smoke","type":"prometheus","access":"proxy","url":"http://127.0.0.1"}',
        ),
    )
    admin_login(run)
    require(
        access.identifiers(get(run, "/api/datasources"), access.SOURCES),
        "4: datasource-identifiers",
    )
    dashboard_checkpoint(run)
    for login in ("", access.VIEWER, access.OWNER):
        require(
            access.contact_points(
                get(run, "/api/v1/provisioning/contact-points", login=login)
            ),
            "4: contact-point-redaction",
        )
        if login != access.OWNER:
            status(
                run,
                "/api/v1/provisioning/contact-points/export?decrypt=true",
                403,
                login=login,
            )
    webhook = dict(run.sentinels.values)[init.WEBHOOK].decode()
    require(
        access.decrypted(
            get(
                run,
                "/api/v1/provisioning/contact-points/export?decrypt=true&format=json",
                login=access.OWNER,
            ),
            webhook,
        ),
        "4: owner-contact-point-export",
    )
    require(
        access.identifiers(get(run, "/api/v1/provisioning/alert-rules"), access.RULES),
        "4: alert-rule-identifiers",
    )
    status(
        run,
        "/api/org/preferences",
        200,
        login=access.OWNER,
        operation=("PUT", json.dumps({"homeDashboardUID": access.HEALTH}).encode()),
    )
    require(
        access.home(get(run, "/api/org/preferences", login=access.OWNER)),
        "4: home-preference",
    )
    require(
        access.home(get(run, "/api/dashboards/home"), redirect=True),
        "4: anonymous-home",
    )
    logs = run.logged(["logs", "--no-log-prefix", "grafana"], "grafana.log")
    require(logs.code == 0, "4: provisioning-log-unavailable")
    require(
        access.provisioning(logs.stdout + logs.stderr) is None, "4: provisioning-log"
    )
    run.emit(
        "ok 4: Grafana permissions, proxy trust, recovery login, "
        "provisioning and redaction hold"
    )


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
    run.loki_write_probe()
    image = present(
        run.model.get("telemetrygen", {}).get("image"), "5: telemetrygen-image"
    )
    network = ["--network", "host"] if run.linux else []
    endpoint = "127.0.0.1:14317" if run.linux else ".".join(("host", "docker", "internal:14317"))
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
    run.wait_ready("loki", 6)
    recheck(run)
    for service in ("tempo", "mimir"):
        require(run.compose(["restart", service]).code == 0, "6: backend-restart")
        run.wait_ready(service, 6)
        recheck(run)
    run.emit(
        "ok 6: buckets hold objects; Loki returns the receipt from its bucket alone; "
        "Tempo and Mimir keep it across restart"
    )
