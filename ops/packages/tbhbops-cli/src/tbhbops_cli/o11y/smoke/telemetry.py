"""Verify handling of delta and cumulative telemetry."""

from typing import TYPE_CHECKING

from tbhbops.o11y import smoke_query as query
from tbhbops.o11y.receipt import decode

from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke.safety import present, require

if TYPE_CHECKING:
    from tbhbops_cli.o11y.smoke.runtime import Smoke


from tbhbops_cli.o11y.smoke import queries, readiness
from tbhbops_cli.o11y.smoke.transport import HTTP_OK, OTLP, request

DELTA_TIMEOUT = 60


def discarded(run: Smoke) -> float:
    result = run.inside("http://mimir:8080/metrics", body=True)
    body, separator, status = result.stdout.rstrip("\n").rpartition("\n")
    require(
        result.code == 0 and bool(separator) and status == "200", "11: discard-scrape"
    )
    return present(query.discards(body), "11: discard-exposition")


def post(run: Smoke, signal: str, body: bytes) -> bool:
    code, _ = request(run, "/v1/" + signal, method="POST", data=body, origin=OTLP)
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
            queries.mimir(run, f'o11y_smoke_{name}_total{{run_id="{identity}"}}')
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
    readiness.wait_ready(run, "alloy", 11)
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
