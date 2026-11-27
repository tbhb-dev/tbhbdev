"""Make bounded HTTP requests to the smoke stack."""

from typing import TYPE_CHECKING

from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError

if TYPE_CHECKING:
    from tbhbops_cli.o11y.smoke.runtime import Smoke

GRAFANA = "http://127.0.0.1:13000"
OTLP = "http://127.0.0.1:14318"
HTTP_OK = 200
REQUEST_TIMEOUT = 15


def request(
    run: Smoke,
    path: str,
    *,
    login: str = "",
    method: str = "GET",
    data: bytes | None = None,
    origin: str = GRAFANA,
    timeout: int = REQUEST_TIMEOUT,
) -> tuple[int, bytes]:
    headers = {"Tailscale-User-Login": login} if login else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        return run.system.receipt_http.request(
            method, origin + path, data, headers, timeout
        )
    except BodyTooLargeError, TransportError:
        return 0, b""
