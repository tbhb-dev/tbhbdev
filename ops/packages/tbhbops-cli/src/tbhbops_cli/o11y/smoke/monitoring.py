"""Verify backend self-monitoring and RustFS metrics."""

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from tbhbops.o11y import rustfs_probe
from tbhbops.o11y import smoke_query as query
from tbhbops.o11y.receipt import decode

from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke.safety import (
    SmokeError,
    require,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbops_cli.o11y.smoke.runtime import Smoke


from tbhbops_cli.o11y.smoke import queries, readiness
from tbhbops_cli.o11y.smoke.transport import HTTP_OK, request

STALE_AGE = 90
MIN_COUNTER_SAMPLES = 2
SCRAPE_TIMEOUT = 180
COUNTER_TIMEOUT = 90
STOPPED_TIMEOUT = 120
RULE_TIMEOUT = 60
PUSH_TIMEOUT = 120


def panel(run: Smoke, title: str) -> list[str] | None:
    code, body = request(run, "/api/dashboards/uid/o11y-health")
    if code != HTTP_OK:
        return None
    expression = query.panel_expression(body, title)
    return (
        query.values(queries.mimir(run, expression)) if expression is not None else None
    )


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


def group7(run: Smoke, source: Path) -> None:
    config = run.fs.read_text(source / "config/alloy/config.alloy")
    limits = query.scrape_limits(config)
    jobs = tuple(limits)
    require(
        poll(
            run.system,
            SCRAPE_TIMEOUT,
            lambda: query.all_up(queries.mimir(run, "up == 1"), jobs),
        ),
        "7: scrape-jobs-up",
    )
    for job, limit in limits.items():
        require(
            len(
                query.vector(
                    queries.mimir(
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
    queries.helper_receipt(run, "7", traces=True)

    def counter() -> bool:
        values = query.values(
            queries.mimir(
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
    evaluated = queries.evaluate(run, "o11y-service-down")
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
            lambda: queries.fires(run, "o11y-service-down", "job", ["o11y/tempo"]),
        ),
        "7: stopped-tempo-rule",
    )
    require(run.compose(["start", "tempo"]).code == 0, "7: tempo-start")
    readiness.wait_ready(run, "tempo", 7)
    require(run.compose(["stop", "alloy"]).code == 0, "7: alloy-stop")
    require(
        poll(run.system, SCRAPE_TIMEOUT, lambda: stale(run, jobs)), "7: stale-panels"
    )
    require(run.compose(["start", "alloy"]).code == 0, "7: alloy-start")
    readiness.wait_ready(run, "alloy", 7)
    require(
        poll(
            run.system,
            SCRAPE_TIMEOUT,
            lambda: query.all_up(queries.mimir(run, "up == 1"), jobs),
        ),
        "7: recovered-scrape-jobs",
    )
    rustfs(run, config)
    run.emit(
        "ok 7: scrape identities and limits, stopped and stale "
        "panels, service-down and RustFS self-monitoring pass"
    )


def rustfs_pushed(run: Smoke) -> bool:
    rows = query.vector(queries.mimir(run, 'rustfs_start_total{job="o11y/rustfs"}'))
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
    rows = query.vector(queries.mimir(run, 'target_info{job="o11y/rustfs"}'))
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
    body = queries.get(
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
    body = queries.get(
        run,
        "/api/datasources/proxy/uid/tempo/api/search?"
        + urlencode({"q": '{resource.service.name="rustfs"}'}),
        "7",
    )
    require(decode(body).get("traces") == [], "7: rustfs-no-traces")
    body = queries.get(
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
