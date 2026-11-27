"""Verify alert rule conditions and delivery to local receivers."""

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from tbhbops.o11y import smoke
from tbhbops.o11y import smoke_query as query
from tbhbops.o11y.receipt import decode

from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke import queries, telemetry
from tbhbops_cli.o11y.smoke.safety import SmokeError, require, scan_path

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbops_cli.o11y.smoke.runtime import Smoke

RULE_TIMEOUT = 60
PUSH_TIMEOUT = 120
WEBHOOK_TIMEOUT = 480
MAIL_TIMEOUT = 120
WEBHOOK_POSTS = str(PurePosixPath("/") / "tmp" / "posts")
MAIL_MESSAGES = "http://127.0.0.1:8025/api/v1/messages"


def smtp_diagnostics(run: Smoke) -> None:
    """Search both raw captures before publishing any SMTP failure evidence."""
    grafana = run.compose(["logs", "--no-color", "grafana"])
    mail = run.compose(
        ["exec", "-T", "alert-mail", "wget", "-qO-", MAIL_MESSAGES], tools=True
    )
    for result in (grafana, mail):
        require(result.code == 0, "diagnostics: SMTP capture unavailable")
        require(
            smoke.scan(
                (result.stdout + result.stderr).encode(errors="surrogateescape"),
                run.sentinels.scan_values(),
            )
            is None,
            "diagnostics: SMTP capture unsafe",
        )
    body = decode(mail.stdout.encode(errors="surrogateescape"))
    count = body.get("total") if isinstance(body, dict) else None
    require(type(count) is int and count >= 0, "diagnostics: mail count unavailable")
    lines = [
        line
        for line in (grafana.stdout + grafana.stderr).splitlines(keepends=True)
        if any(word in line.lower() for word in ("smtp", "email", "notify"))
    ]
    run.fs.write_atomic(
        run.artifacts / "grafana-smtp.log",
        "".join(lines) or "no SMTP, email or notify log lines\n",
        0o600,
    )
    run.fs.write_atomic(
        run.artifacts / "mailpit-count.log", f"messages={count}\n", 0o600
    )


def group12(run: Smoke, source: Path) -> None:
    titles = queries.rule_titles(run, source)
    # A non-200 inside the poll still fails at once via queries.get(): only an
    # unhealthy rule state is retried, since alloy was just restarted.
    require(
        poll(
            run.system,
            RULE_TIMEOUT,
            lambda: query.healthy_rules(
                queries.get(run, queries.RULES_PATH, "12"), titles
            ),
        ),
        "12: rule-health",
    )
    baseline: tuple[tuple[str, list[str], int | None], ...] = (
        ("disk-low", ["o11y/rustfs"], None),
        ("rustfs-memory", [], None),
        ("rustfs-memory", ["o11y/rustfs"], 0),
        ("ingest-dropped", [], None),
        ("alloy-export", [], None),
        ("log-rate", [], None),
        ("log-rate", [], 0),
    )
    for uid, expected, threshold in baseline:
        rows = queries.evaluate(run, "o11y-" + uid, threshold)
        matches = query.firing(rows, "job", expected)
        if not matches:
            run.emit(
                f"smoke diagnostics: rule=o11y-{uid} threshold={threshold} "
                f"expected={expected} observed={query.observed_labels(rows, 'job')}"
            )
        require(matches, "12: baseline-conditions")
        if uid in {"rustfs-memory", "log-rate"}:
            require(rows is not None and len(rows) == 1, "12: baseline-series-count")
    require(
        telemetry.post(
            run, "logs", query.logs(int(run.system.epoch()) - 8 * 86400, old=True)
        ),
        "12: old-log-upload",
    )

    def ingest() -> bool:
        rows = queries.evaluate(run, "o11y-ingest-dropped")
        return query.firing(rows, "job", ["o11y/loki"]) and query.firing(
            rows, "reason", ["greater_than_max_sample_age"]
        )

    require(poll(run.system, PUSH_TIMEOUT, ingest), "12: ingest-discard-reason")
    require(
        poll(run.system, PUSH_TIMEOUT, lambda: export_discarded(run)),
        "12: export-discard-counter",
    )
    require(queries.quiet(run, "o11y-alloy-export"), "12: isolated-export-loss")
    require(
        telemetry.post(run, "logs", query.logs(int(run.system.epoch()), old=False)),
        "12: rate-log-upload",
    )
    require(
        poll(
            run.system,
            PUSH_TIMEOUT,
            lambda: (
                queries.evaluate(run, "o11y-log-rate", 0)
                == [{"labels": {}, "values": [1]}]
            ),
        ),
        "12: positive-log-rate",
    )
    rows = queries.evaluate(run, "o11y-log-rate")
    require(
        query.quiet(rows) and rows is not None and len(rows) == 1, "12: quiet-log-rate"
    )

    def notified() -> bool:
        result = run.compose(
            ["exec", "-T", "alert-stub", "cat", WEBHOOK_POSTS], tools=True
        )
        return result.code == 0 and query.webhook(
            result.stdout, run.sentinels.token.decode()
        )

    require(poll(run.system, WEBHOOK_TIMEOUT, notified), "12: webhook-delivery")

    def mailed() -> bool:
        result = run.compose(
            [
                "exec",
                "-T",
                "alert-mail",
                "wget",
                "-qO-",
                MAIL_MESSAGES,
            ],
            tools=True,
        )
        return result.code == 0 and query.mailed(result.stdout.encode())

    delivered = poll(run.system, MAIL_TIMEOUT, mailed)
    if not delivered:
        try:
            smtp_diagnostics(run)
        except SmokeError, OSError:
            run.emit("smoke diagnostics: SMTP capture or scan unavailable")
    require(delivered, "12: smtp-delivery")
    require(
        run.logged(["logs", "--no-color", "grafana"], "grafana.log").code == 0,
        "12: grafana-log-read",
    )
    scan_path(run.fs, run.out / "grafana.log", run.sentinels.scan_values())
    run.emit(
        "ok 12: alert conditions isolate their targets; disk "
        "notifications reach the seeded webhook and SMTP stubs"
    )


def export_discarded(run: Smoke) -> bool:
    rows = query.vector(
        queries.mimir(
            run,
            'otelcol_exporter_send_failed_log_records_total{job="o11y/alloy",'
            'component_id="otelcol.exporter.otlphttp.loki"} > 0',
        )
    )
    return rows is not None and len(rows) == 1
