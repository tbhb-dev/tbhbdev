"""The smoke HTTP transport seam."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.smoke import (
    transport,
)
from unit.o11y.smoke.grafana_fixture import instance_b


def test_request_seam_contract() -> None:
    _, _, http, run = instance_b()
    transport.request(run, "/api/org", login="viewer", method="POST", data=b"payload")
    assert http.calls[-1] == (
        "POST",
        "http://127.0.0.1:13000/api/org",
        b"payload",
        {"Tailscale-User-Login": "viewer", "Content-Type": "application/json"},
        15,
    )
    for error in (BodyTooLargeError(), TransportError("private-body")):
        http.overrides["/api/org"] = error
        assert transport.request(run, "/api/org") == (0, b"")
