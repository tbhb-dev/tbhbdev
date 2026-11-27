"""HTTP queries and groups 7, 11 and 12 through the smoke seams."""

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlencode
lazy from collections.abc import Sequence

import yaml
from tbhbops.o11y import rustfs_probe, smoke
from tbhbops.o11y import smoke_query as query
from tbhbops.o11y.receipt import decode

from tbhbops_cli.o11y import receipt, smoke_stages
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke import SmokeError, present, require, scan_path

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbops.o11y.validate import Document

    from tbhbops_cli.o11y.smoke import Smoke

HTTP_OK = 200
STALE_AGE = 90
MIN_COUNTER_SAMPLES = 2
REQUEST_TIMEOUT = 15
EVAL_TIMEOUT = 30
SCRAPE_TIMEOUT = 180
COUNTER_TIMEOUT = 90
STOPPED_TIMEOUT = 120
RULE_TIMEOUT = 60
PUSH_TIMEOUT = 120
WEBHOOK_TIMEOUT = 480
MAIL_TIMEOUT = 120
DELTA_TIMEOUT = 60
RULES_PATH = "/api/prometheus/grafana/api/v1/rules"
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


def request(
    run: Smoke,
    path: str,
    *,
    data: bytes | None = None,
    timeout: int = REQUEST_TIMEOUT,
    otlp: bool = False,
) -> tuple[int, bytes]:
    try:
        return run.system.receipt_http.request(
            "GET" if data is None else "POST",
            (smoke_stages.OTLP if otlp else smoke_stages.GRAFANA) + path,
            data,
            {"Content-Type": "application/json"} if data is not None else {},
            timeout,
        )
    except BodyTooLargeError, TransportError:
        return 0, b""


def get(run: Smoke, path: str, group: str) -> bytes:
    code, body = request(run, path)
    require(code == HTTP_OK, group + ": query-http")
    return body


def mimir(run: Smoke, expression: str, *, time: int | None = None) -> bytes:
    params = {"query": expression}
    if time is not None:
        params["time"] = str(time)
    code, body = request(
        run, "/api/datasources/proxy/uid/mimir/api/v1/query?" + urlencode(params)
    )
    return body if code == HTTP_OK else b""


def evaluate(
    run: Smoke, uid: str, threshold: int | None = None
) -> list[Document] | None:
    code, body = request(run, "/api/v1/provisioning/alert-rules/" + uid)
    if code != HTTP_OK:
        return None
    prepared = query.evaluation_request(body, threshold)
    if prepared is None:
        return None
    condition, data = prepared
    code, body = request(run, "/api/v1/eval", data=data, timeout=EVAL_TIMEOUT)
    return query.evaluated(body, condition) if code == HTTP_OK else None


def fires(
    run: Smoke,
    uid: str,
    label: str,
    expected: Sequence[str | None],
    threshold: int | None = None,
) -> bool:
    return query.firing(evaluate(run, uid, threshold), label, expected)


def quiet(run: Smoke, uid: str) -> bool:
    return query.quiet(evaluate(run, uid))


def panel(run: Smoke, title: str) -> list[str] | None:
    code, body = request(run, "/api/dashboards/uid/o11y-health")
    if code != HTTP_OK:
        return None
    expression = query.panel_expression(body, title)
    return query.values(mimir(run, expression)) if expression is not None else None


def recent(values: list[str] | None, *, stale: bool = False) -> bool:
    try:
        return (stale and values == []) or (
            values is not None
            and len(values) == 1
            and (
                float(values[0]) >= STALE_AGE if stale else float(values[0]) < STALE_AGE
            )
        )
    except ValueError, TypeError:
        return False


def tempo_failed(run: Smoke) -> bool:
    return (
        panel(run, "Up: o11y/tempo") == ["0"]
        and recent(panel(run, "Last scrape: o11y/tempo"))
        and panel(run, "Restarts in 1h: o11y/tempo") == []
        and panel(run, "Tempo refused and discarded spans") == []
    )


def stale(run: Smoke, jobs: tuple[str, ...]) -> bool:
    return (
        all(
            panel(run, "Up: " + job) == []
            and recent(panel(run, "Last scrape: " + job), stale=True)
            and panel(run, "Restarts in 1h: " + job) == []
            for job in jobs
        )
        and panel(run, "Alloy client receiver refusals") == []
    )


def helper_receipt(run: Smoke, group: str, *, traces: bool = False) -> None:
    lines, passed = receipt.receipt(
        run.system,
        smoke_stages.OTLP,
        smoke_stages.GRAFANA,
        signals="traces" if traces else "traces,logs,metrics",
        safe_failures=True,
    )
    for line in lines:
        run.emit(line)
    require(passed, group + ": receipt")


def group7(run: Smoke, source: Path) -> None:
    config = run.fs.read_text(source / "config/alloy/config.alloy")
    limits = query.scrape_limits(config)
    jobs = tuple(limits)
    require(
        poll(
            run.system,
            SCRAPE_TIMEOUT,
            lambda: query.all_up(mimir(run, "up == 1"), jobs),
        ),
        "7: scrape-jobs-up",
    )
    for job, limit in limits.items():
        require(
            len(
                query.vector(
                    mimir(
                        run,
                        f'scrape_samples_post_metric_relabeling{{job="{job}"}}'
                        f" < {limit}",
                    )
                )
                or []
            )
            == 1,
            "7: scrape-sample-limit",
        )
    helper_receipt(run, "7", traces=True)

    def counter() -> bool:
        values = query.values(
            mimir(
                run,
                'count_over_time(tempo_receiver_refused_spans{job="o11y/tempo"}[2m])',
            )
        )
        try:
            return (
                values is not None
                and len(values) == 1
                and float(values[0]) >= MIN_COUNTER_SAMPLES
            )
        except ValueError, TypeError:
            return False

    require(poll(run.system, COUNTER_TIMEOUT, counter), "7: refused-counter-samples")
    # The service-down rule has a row for o11y/rustfs only once RustFS has
    # reported, so the inventory below needs the push first.
    require(
        poll(run.system, PUSH_TIMEOUT, lambda: rustfs_pushed(run)),
        "7: rustfs-push-identity",
    )
    evaluated = evaluate(run, "o11y-service-down")
    require(
        query.quiet(evaluated)
        and evaluated is not None
        and len(evaluated) == len(jobs) + 1,
        "7: service-down-coverage",
    )
    require(run.compose(["stop", "tempo"]).code == 0, "7: tempo-stop")
    require(
        poll(run.system, STOPPED_TIMEOUT, lambda: tempo_failed(run)),
        "7: stopped-tempo-panels",
    )
    require(
        poll(
            run.system,
            RULE_TIMEOUT,
            lambda: fires(run, "o11y-service-down", "job", ["o11y/tempo"]),
        ),
        "7: stopped-tempo-rule",
    )
    require(run.compose(["start", "tempo"]).code == 0, "7: tempo-start")
    run.wait_ready("tempo", 7)
    require(run.compose(["stop", "alloy"]).code == 0, "7: alloy-stop")
    require(
        poll(run.system, SCRAPE_TIMEOUT, lambda: stale(run, jobs)), "7: stale-panels"
    )
    require(run.compose(["start", "alloy"]).code == 0, "7: alloy-start")
    run.wait_ready("alloy", 7)
    require(
        poll(
            run.system,
            SCRAPE_TIMEOUT,
            lambda: query.all_up(mimir(run, "up == 1"), jobs),
        ),
        "7: recovered-scrape-jobs",
    )
    rustfs(run, config)
    run.emit(
        "ok 7: scrape identities and limits, stopped and stale "
        "panels, service-down and RustFS self-monitoring pass"
    )


def rustfs_pushed(run: Smoke) -> bool:
    rows = query.vector(mimir(run, 'rustfs_start_total{job="o11y/rustfs"}'))
    return (
        rows is not None
        and len(rows) == 1
        and all(
            rows[0].get("metric", {}).get(k) == v
            for k, v in (
                ("instance", "rustfs:9000"),
                ("namespace", "o11y"),
                ("cluster", "o11y"),
            )
        )
    )


def rustfs(run: Smoke, config: str) -> None:
    rows = query.vector(mimir(run, 'target_info{job="o11y/rustfs"}'))
    require(
        rows is not None
        and len(rows) == 1
        and rows[0].get("metric", {}).get("host_name") == "smoke-host"
        and rows[0]["metric"].get("deployment_environment_name") == "smoke"
        and not {"rustfs_node", "network_local_address"}.intersection(
            rows[0]["metric"]
        ),
        "7: rustfs-resource-identity",
    )
    block = config.split('otelcol.processor.filter "self" {', 1)[1].partition("\n}")[0]
    allowlist = set(re.findall(r'"(rustfs_[a-z0-9_]+)"', block))
    body = get(
        run,
        "/api/datasources/proxy/uid/mimir/api/v1/series?"
        + urlencode({"match[]": '{job="o11y/rustfs"}'}),
        "7",
    )
    try:
        names = {
            re.sub(r"_(bucket|sum|count)$", "", s["__name__"])
            for s in decode(body)["data"]
        } - {"target_info"}
    except KeyError, TypeError:
        message = "7: rustfs-metric-allowlist"
        raise SmokeError(message) from None
    require(bool(names) and names <= allowlist, "7: rustfs-metric-allowlist")
    body = get(
        run,
        "/api/datasources/proxy/uid/tempo/api/search?"
        + urlencode({"q": '{resource.service.name="rustfs"}'}),
        "7",
    )
    require(decode(body).get("traces") == [], "7: rustfs-no-traces")
    body = get(
        run,
        "/api/datasources/proxy/uid/loki/loki/api/v1/query_range?"
        + urlencode(
            {
                "query": '{service_name="rustfs"}',
                "start": str((int(run.system.epoch()) - 3600) * 1000000000),
            }
        ),
        "7",
    )
    require(query.vector(body) == [], "7: rustfs-no-otlp-logs")
    result = run.logged(["logs", "--no-log-prefix", "rustfs"], "rustfs.log")
    require(result.code == 0, "7: rustfs-log-read")
    text = result.stdout + result.stderr
    require(rustfs_probe.start_log(text) is None, "7: rustfs-start-log")
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("Starting: ")]
    require(
        bool(starts) and all(line.startswith("{") for line in lines[starts[0] + 1 :]),
        "7: rustfs-json-after-start",
    )


def discarded(run: Smoke) -> float:
    result = run.inside("http://mimir:8080/metrics", body=True)
    body, separator, status = result.stdout.rstrip("\n").rpartition("\n")
    require(
        result.code == 0 and bool(separator) and status == "200", "11: discard-scrape"
    )
    return present(query.discards(body), "11: discard-exposition")


def post(run: Smoke, signal: str, body: bytes) -> bool:
    code, _ = request(run, "/v1/" + signal, data=body, otlp=True)
    return code == HTTP_OK


def group11(run: Smoke) -> None:
    args = run.inspect("alloy", "{{json .Args}}")
    require(
        args.code == 0
        and "--stability.level=experimental" in decode(args.stdout.encode()),
        "11: alloy-stability",
    )
    identity = run.system.random_bytes(8).hex()
    before = discarded(run)
    now = int(run.system.epoch())
    for name, temporality, value, times in (
        ("delta", 1, 3, (now - 20, now - 10)),
        ("delta", 1, 4, (now - 10, now)),
        ("cumulative", 2, 5, (now - 20, now)),
    ):
        require(
            post(
                run,
                "metrics",
                query.sum_point(
                    "o11y_smoke_" + name, temporality, value, times, identity
                ),
            ),
            "11: point-upload",
        )

    def value(name: str, expected: str) -> bool:
        return query.values(
            mimir(run, f'o11y_smoke_{name}_total{{run_id="{identity}"}}')
        ) == [expected]

    require(
        poll(
            run.system,
            DELTA_TIMEOUT,
            lambda: value("delta", "7") and value("cumulative", "5"),
        ),
        "11: delta-cumulative-values",
    )
    require(discarded(run) == before, "11: no-parse-discards")
    require(run.compose(["restart", "alloy"]).code == 0, "11: alloy-restart")
    run.wait_ready("alloy", 11)
    require(
        poll(run.system, DELTA_TIMEOUT, lambda: post(run, "metrics", b"{}")),
        "11: otlp-listening",
    )
    now = int(run.system.epoch())
    require(
        post(
            run,
            "metrics",
            query.sum_point("o11y_smoke_delta", 1, 2, (now - 5, now), identity),
        ),
        "11: restarted-point-upload",
    )
    require(
        poll(run.system, DELTA_TIMEOUT, lambda: value("delta", "2")),
        "11: restarted-delta-value",
    )
    require(discarded(run) == before, "11: restarted-no-parse-discards")
    run.emit(
        "ok 11: delta sums accumulate, cumulative sums pass through, "
        "restart resets conversion and Mimir discards none"
    )


def rule_titles(run: Smoke, source: Path, *, capacity: bool = False) -> tuple[str, ...]:
    rules = yaml.safe_load(
        run.fs.read_text(source / "config/grafana/provisioning/alerting/rules.yaml")
    )
    return tuple(
        r["title"]
        for g in rules["groups"]
        if not capacity or g["name"] == "agent-capacity"
        for r in g["rules"]
    )


def group12(run: Smoke, source: Path) -> None:
    titles = rule_titles(run, source)
    # A non-200 inside the poll still fails at once via get(): only an
    # unhealthy rule state is retried, since alloy was just restarted.
    require(
        poll(
            run.system,
            RULE_TIMEOUT,
            lambda: query.healthy_rules(get(run, RULES_PATH, "12"), titles),
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
        rows = evaluate(run, "o11y-" + uid, threshold)
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
        post(run, "logs", query.logs(int(run.system.epoch()) - 8 * 86400, old=True)),
        "12: old-log-upload",
    )

    def ingest() -> bool:
        rows = evaluate(run, "o11y-ingest-dropped")
        return query.firing(rows, "job", ["o11y/loki"]) and query.firing(
            rows, "reason", ["greater_than_max_sample_age"]
        )

    require(poll(run.system, PUSH_TIMEOUT, ingest), "12: ingest-discard-reason")
    require(
        poll(run.system, PUSH_TIMEOUT, lambda: export_discarded(run)),
        "12: export-discard-counter",
    )
    require(quiet(run, "o11y-alloy-export"), "12: isolated-export-loss")
    require(
        post(run, "logs", query.logs(int(run.system.epoch()), old=False)),
        "12: rate-log-upload",
    )
    require(
        poll(
            run.system,
            PUSH_TIMEOUT,
            lambda: (
                evaluate(run, "o11y-log-rate", 0) == [{"labels": {}, "values": [1]}]
            ),
        ),
        "12: positive-log-rate",
    )
    rows = evaluate(run, "o11y-log-rate")
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
        mimir(
            run,
            'otelcol_exporter_send_failed_log_records_total{job="o11y/alloy",'
            'component_id="otelcol.exporter.otlphttp.loki"} > 0',
        )
    )
    return rows is not None and len(rows) == 1
