"""Query Grafana, Mimir, alert rules, and telemetry receipts."""

from typing import TYPE_CHECKING
from urllib.parse import urlencode
lazy from collections.abc import Sequence

import yaml
from tbhbops.o11y import smoke_query as query

from tbhbops_cli.o11y import receipt
from tbhbops_cli.o11y.smoke.safety import require
from tbhbops_cli.o11y.smoke.transport import GRAFANA, HTTP_OK, OTLP, request

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbops.o11y.validate import Document

    from tbhbops_cli.o11y.smoke.runtime import Smoke

EVAL_TIMEOUT = 30
RULES_PATH = "/api/prometheus/grafana/api/v1/rules"


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
    code, body = request(
        run, "/api/v1/eval", method="POST", data=data, timeout=EVAL_TIMEOUT
    )
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


def helper_receipt(run: Smoke, group: str, *, traces: bool = False) -> None:
    lines, passed = receipt.receipt(
        run.system,
        OTLP,
        GRAFANA,
        signals="traces" if traces else "traces,logs,metrics",
        safe_failures=True,
    )
    for line in lines:
        run.emit(line)
    require(passed, group + ": receipt")


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
