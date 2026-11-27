"""HTTP methods used by the smoke admin transport."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import pytest
from tbhbops_cli.o11y.http import LocalAdminHttp
from test_o11y_admin_http import Response, connection


def test_admin_post_method(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = connection(monkeypatch, Response(b"ok"))
    assert LocalAdminHttp().request(
        "http://localhost:123/reload", b"fixture", 30, method="POST"
    ) == (201, b"ok")
    assert constructor.return_value.request.call_args.args == ("POST", "/reload")
