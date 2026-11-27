"""Verify Grafana access, recovery, and dashboard provisioning."""

import json
from typing import TYPE_CHECKING

from tbhbops.o11y import init
from tbhbops.o11y import smoke_access as access

from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.smoke.safety import SmokeError, require
from tbhbops_cli.o11y.smoke.transport import GRAFANA, HTTP_OK, REQUEST_TIMEOUT, request

if TYPE_CHECKING:
    from tbhbops_cli.o11y.smoke.runtime import Smoke


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
