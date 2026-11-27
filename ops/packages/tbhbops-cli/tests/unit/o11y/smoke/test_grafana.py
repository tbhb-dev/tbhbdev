"""Grafana access, dashboard provisioning, and recovery."""

from dataclasses import replace

import pytest
from tbhbops.o11y import smoke_access as access
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    diagnostics,
    grafana,
    safety,
    transport,
)
from unit.o11y.smoke.grafana_fixture import Admin, instance_b, packed
from unit.o11y.smoke.runtime_fixture import HOST, OUT
from unit.o11y.smoke.stage_contracts import check_export_protocol


def test_group4_password_and_proxy_contract() -> None:
    fs, engine, http, run = instance_b()
    password: bytes = fs.read_bytes(HOST / "secrets/grafana_admin_password")
    grafana.group4(run)
    assert isinstance(run.system.admin_http, Admin)
    assert len(run.system.admin_http.calls) == 2
    assert run.system.admin_http.calls == [
        (transport.GRAFANA + "/api/admin/settings", password, 15),
        (transport.GRAFANA + "/api/admin/settings", b"admin", 15),
    ]
    assert password.decode().strip() not in str(engine.calls)
    assert password.decode().strip() not in str(run.transcript)
    run.finalize_transcript()
    assert password.strip() not in fs.read_bytes(OUT / "transcript.log")
    for login in (access.OWNER, access.VIEWER, "admin"):
        assert any(
            call[1].endswith("/api/user") and call[3] == {"Tailscale-User-Login": login}
            for call in http.calls
        )
    assert (
        run.transcript[-1] == "ok 4: Grafana permissions, proxy trust, recovery login, "
        "provisioning and redaction hold\n"
    )


@pytest.mark.parametrize(
    "answer",
    [(500, b"private-body"), TransportError("private-password"), BodyTooLargeError()],
)
def test_admin_failure_never_prints_password(
    answer: tuple[int, bytes] | Exception,
) -> None:
    fs, engine, _, run = instance_b()
    password: bytes = fs.read_bytes(HOST / "secrets/grafana_admin_password")
    assert isinstance(run.system.admin_http, Admin)
    run.system.admin_http.answer = answer
    with pytest.raises(safety.SmokeError, match=r"^4: recovery-login-status$"):
        grafana.admin_login(run)
    assert password.decode().strip() not in str(engine.calls)
    assert password.decode().strip() not in str(run.transcript)


@pytest.mark.parametrize(
    "answer",
    [(500, b"private-body"), TransportError("private-error"), BodyTooLargeError()],
)
def test_http_failures_are_named(answer: tuple[int, bytes] | Exception) -> None:
    _, _, http, run = instance_b()
    http.overrides["/api/org"] = answer
    with pytest.raises(safety.SmokeError, match=r"^4: grafana-permission-status$"):
        grafana.group4(run)
    with pytest.raises(safety.SmokeError, match=r"^4: grafana-query-unavailable$"):
        grafana.get(run, "/api/org")


def test_admin_desktop_and_file_ownership_failures() -> None:
    fs, engine, _, run = instance_b()
    run.system = replace(run.system, platform="darwin")
    grafana.admin_login(run)
    run.system = replace(run.system, platform="linux")
    engine.overrides["123:456 /d/secrets/grafana_admin_password"] = Result(1)
    with pytest.raises(safety.SmokeError, match=r"^4: recovery-file-owner$"):
        grafana.admin_login(run)
    engine.overrides.clear()
    engine.overrides["472:472 /d/secrets/grafana_admin_password"] = Result(1)
    with pytest.raises(safety.SmokeError, match=r"^4: recovery-file-restore$"):
        grafana.admin_login(run)
    assert fs.read_bytes(HOST / "secrets/grafana_admin_password")


@pytest.mark.parametrize(
    ("classifier", "rule"),
    [
        ("user", "proxy-user"),
        ("orgs", "proxy-org-role"),
        ("owner_model", "owner-service-model"),
        ("whitelist", "proxy-whitelist"),
        ("identifiers", "datasource-identifiers"),
        ("dashboard_checkpoint", "dashboard-checkpoint"),
        ("contact_points", "contact-point-redaction"),
        ("decrypted", "owner-contact-point-export"),
        ("home", "home-preference"),
    ],
)
def test_group4_classifier_failure_rules(
    classifier: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, run = instance_b()

    def reject(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(access, classifier, reject)
    with pytest.raises(safety.SmokeError, match=r"^4: " + rule + "$"):
        grafana.group4(run)


@pytest.mark.parametrize("meta", [None, [], "malformed", 42, True])
def test_non_mapping_dashboard_meta_fails_rule(meta: object) -> None:
    _, _, http, run = instance_b()
    uid = access.AGENTS[0]
    http.overrides[f"/api/dashboards/uid/{uid}"] = (
        200,
        packed({"dashboard": {"uid": uid}, "meta": meta}),
    )
    with pytest.raises(safety.SmokeError, match=r"^4: dashboard-checkpoint$"):
        grafana.group4(run)


@pytest.mark.parametrize(
    ("match", "body", "rule"),
    [
        ("/api/v1/provisioning/alert-rules", b"[]", "alert-rule-identifiers"),
        ("/api/dashboards/home", b"{}", "anonymous-home"),
    ],
)
def test_group4_remaining_http_rules(match: str, body: bytes, rule: str) -> None:
    _, _, http, run = instance_b()
    http.overrides[match] = (200, body)
    with pytest.raises(safety.SmokeError, match=r"^4: " + rule + "$"):
        grafana.group4(run)


@pytest.mark.parametrize(
    ("match", "result", "rule"),
    [
        (
            "{{.State.Status}} {{.State.ExitCode}}",
            Result(17, "exited 0"),
            "owner-service-exit",
        ),
        (
            "{{.State.Status}} {{.State.ExitCode}}",
            Result(0, "running 0"),
            "owner-service-exit",
        ),
        ("GF_AUTH_PROXY_WHITELIST", Result(17, "127.0.0.1"), "proxy-whitelist"),
        ("http://grafana:3000/api/user", Result(0, "200"), "peer-proxy-permission"),
        ("http://grafana:3000/api/user", Result(17, "401"), "peer-proxy-permission"),
        (
            "logs --no-log-prefix grafana",
            Result(17, "provisioning"),
            "provisioning-log-unavailable",
        ),
        (
            "logs --no-log-prefix grafana",
            Result(0, "provisioning level=error private-body"),
            "provisioning-log",
        ),
    ],
)
def test_group4_process_failure_rules(match: str, result: Result, rule: str) -> None:
    _, engine, _, run = instance_b()
    engine.overrides[match] = result
    with pytest.raises(safety.SmokeError, match=r"^4: " + rule + "$"):
        grafana.group4(run)


def test_proxy_recovery_contract() -> None:
    _, _, http, run = instance_b()
    grafana.proxy_role(run, "admin", "Admin", recovery=True)
    assert [call[3] for call in http.calls[-2:]] == [
        {"Tailscale-User-Login": "admin"}
    ] * 2
    http.overrides["/api/user"] = (200, b'{"login":"admin","isGrafanaAdmin":false}')
    with pytest.raises(safety.SmokeError, match=r"^4: proxy-user$"):
        grafana.proxy_role(run, "admin", "Admin", recovery=True)


def test_recovery_keeps_stored_org_role() -> None:
    _, _, http, run = instance_b()
    http.overrides["/api/user/orgs"] = (200, b'[{"role":"Admin"}]')
    grafana.proxy_role(run, "admin", "Admin", recovery=True)
    with pytest.raises(safety.SmokeError, match=r"^4: proxy-org-role$"):
        grafana.proxy_role(run, access.OWNER, "Admin")


@pytest.mark.parametrize(
    ("text", "passes"), [("401\n", True), ("401 ", False), ("401X", False)]
)
def test_peer_status_strips_only_trailing_newline(text: str, passes: bool) -> None:
    _, engine, _, run = instance_b()
    engine.overrides["http://grafana:3000/api/user"] = Result(0, text)
    if passes:
        grafana.group4(run)
    else:
        with pytest.raises(safety.SmokeError, match=r"^4: peer-proxy-permission$"):
            grafana.group4(run)


def test_grafana_logs_are_private_and_scanned_for_password() -> None:
    fs, engine, _, run = instance_b()
    password: bytes = fs.read_bytes(HOST / "secrets/grafana_admin_password")
    engine.overrides["logs --no-log-prefix grafana"] = Result(
        0, "provisioning level=info " + password.decode()
    )
    grafana.group4(run)
    assert fs.read_bytes(OUT / "grafana.log").endswith(password)
    assert ("write", OUT / "grafana.log", 0o600) in fs.calls
    run.finalize_transcript()
    with pytest.raises(
        safety.SmokeError, match=r"^sentinel secret: grafana.log$"
    ) as failure:
        diagnostics.scans(run)
    assert password.decode().strip() not in str(failure.value)
    assert password.decode().strip() not in "".join(run.transcript)
    assert password.decode().strip() not in str(engine.calls)


def test_export_protocol_contract() -> None:
    check_export_protocol("group4")
