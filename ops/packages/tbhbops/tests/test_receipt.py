# cspell:ignore Bservice Bresource Brun
"""Receipt payloads, URL encoding and the five original Loki cases."""

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from tbhbops.o11y import receipt as r

RUN = "0123456789abcdef"
EMPTY: dict[str, object] = {}
EMPTY_LIST: list[object] = []
TRACE = "1234567890abcdef1234567890abcdef"


def response(
    n: int = 1, stamp: str = "100000000000", body: str = "o11y-check fixture"
) -> bytes:
    return json.dumps(
        {"status": "success", "data": {"result": [{"values": [[stamp, body]] * n}]}}
    ).encode()


def test_transport_http_json_loki() -> None:
    assert (
        r.log_classify(200, b"sensitive", "fixture", 100, "URLError")
        == "case=transport-error transport=URLError"
    )
    assert r.log_classify(500, b"sensitive", "fixture", 100) == "case=http-error"
    for body in (b"sensitive", b"{} {}", b"\xff", b"NaN", b"Infinity"):
        assert r.log_classify(200, body, "fixture", 100) == "case=invalid-json"
    assert (
        r.log_classify(200, b'{"status":"error","error":"sensitive"}', "fixture", 100)
        == "case=loki-error"
    )


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        1,
        {},
        {"status": 3},
        {"status": "success"},
        {"status": "success", "data": []},
        *[
            {"status": "success", "data": {"result": value}}
            for value in (
                None,
                EMPTY,
                [None],
                [EMPTY],
                [{"values": EMPTY}],
                *[
                    [{"values": [line]}]
                    for line in (
                        None,
                        EMPTY,
                        EMPTY_LIST,
                        ["1"],
                        ["1", "body", "extra"],
                        [1, "body"],
                        ["", "body"],
                        ["sensitive", "body"],
                        ["1x", "body"],
                        ["1\n", "body"],
                        ["1", None],
                    )
                ],
            )
        ],
    ],
)
def test_malformed_shapes(body: object) -> None:
    assert (
        r.log_classify(200, json.dumps(body).encode(), "fixture", 100)
        == "case=invalid-response-shape"
    )


def test_zero_multiple_mismatch() -> None:
    assert r.log_classify(200, response(0), "fixture", 100) == "case=zero-lines count=0"
    for count in (2, 11):
        assert (
            r.log_classify(200, response(count), "fixture", 100)
            == f"case=multiple-lines count={count} cap=10"
        )
    assert (
        r.log_classify(200, response(body="sensitive"), "fixture", 100)
        == "case=body-mismatch count=1"
    )


def test_floor_equality() -> None:
    assert r.log_classify(200, response(), "fixture", 100) == "case=accepted count=1"
    assert (
        r.log_classify(200, response(stamp="99999999999"), "fixture", 100)
        == "case=older-than-floor count=1 timestamp_ns=99999999999"
    )


def test_normal_and_recheck_floors() -> None:
    assert (
        r.log_classify(200, response(stamp="0"), "fixture", 0)
        == "case=accepted count=1"
    )
    path = r.query_path("log", "fixture", TRACE, 160, 200)
    assert path == (
        "/api/datasources/proxy/uid/loki/loki/api/v1/query_range?"
        "query=%7Bservice_name%3D%22o11y-check-fixture%22%7D"
        "&start=100000000000&end=260000000000&limit=10"
    )


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:123/", "http://[::1]:123/", "http://localhost:123/path/"]
)
def test_loopback(url: str) -> None:
    assert r.base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:1",
        "http://localhost",
        "http://example.test:1",
        "http://user@localhost:1",
        "http://:password@localhost:1",
        "http://localhost:1/?a=1",
        "http://localhost:1/#f",
        "http://localhost:bad",
        "http://localhost:65536",
    ],
)
def test_url_refusal(url: str) -> None:
    with pytest.raises(ValueError, match=r"loopback|Port"):
        r.base_url(url)


def test_state() -> None:
    assert r.state_line(RUN, TRACE, 30, 100) == f"{RUN} {TRACE} 30 100\n"
    assert r.parse_state(r.state_line(RUN, TRACE, 30, 100)) == (RUN, TRACE, 30, 100)
    assert r.parse_state(f" {RUN.upper()}\t{TRACE.upper()} 0 0\n") == (
        RUN.upper(),
        TRACE.upper(),
        0,
        0,
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"{RUN} {TRACE} 1",
        f"{RUN} {TRACE} 1 1 extra",
        f"{RUN} {TRACE} -1 1",
        f"{RUN} {TRACE} 1 -1",
        f"{RUN} {TRACE} 1.0 1",
        f"{RUN} {TRACE} 1 epoch",
        f"{RUN}0 {TRACE} 1 1",
        f"{RUN[:-1]} {TRACE} 1 1",
        f"{RUN} {TRACE}0 1 1",
        f"{RUN} {TRACE[:-1]} 1 1",
        f"g{RUN[1:]} {TRACE} 1 1",
        f"{RUN} z{TRACE[1:]} 1 1",
    ],
)
def test_state_refusal(text: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid receipt state$"):
        r.parse_state(text)


@pytest.mark.parametrize("signal", ["traces", "logs", "metrics"])
def test_payloads(signal: str) -> None:
    expected = json.loads(
        (Path(__file__).parent / "testdata" / f"receipt-{signal}.json").read_text()
    )
    assert json.loads(r.payload(signal, RUN, TRACE, "span", 123, 100)) == expected


def test_queries() -> None:
    prefix = "/api/datasources/proxy/uid/"
    assert (
        r.query_path("trace", RUN, TRACE, 100, 200)
        == f"{prefix}tempo/api/traces/{TRACE}"
    )
    assert r.query_path("grpc", RUN, TRACE, 100, 200) == (
        f"{prefix}tempo/api/search?q=%7Bresource.service.name%3D%22"
        f"o11y-check-grpc-{RUN}%22%7D&limit=5"
    )
    selector = quote(f'o11y_check_value{{run_id="{RUN}"}}', safe="")
    assert (
        r.query_path("fresh", RUN, TRACE, 100, 200)
        == f"{prefix}mimir/api/v1/query?query={selector}"
    )
    assert (
        r.query_path("timestamp", RUN, TRACE, 100, 200)
        == f"{prefix}mimir/api/v1/query?query=timestamp({selector})"
    )
    assert (
        r.query_path("stored", RUN, TRACE, 100, 200)
        == f"{prefix}mimir/api/v1/query_range?query={selector}&start=40&end=200&step=15"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"bad", False),
        (b"null", False),
        (b"{}", False),
        (b'{"batches":[null]}', False),
        (b'{"batches":[{"resource":{"attributes":[{"key":"service.name"}]}}]}', False),
        (
            b'{"batches":[{"resource":{"attributes":[{"key":"other","value":{"stringValue":"svc"}}]}}]}',
            False,
        ),
        (
            b'{"batches":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"svc"}}]}}]}',
            True,
        ),
        (
            b'{"batches":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"other"}}]}}]}',
            False,
        ),
        (
            b'{"batches":[{"resource":{"attributes":[{"key":"service.name","value":{}},{"key":"service.name","value":{"stringValue":"svc"}}]}}]}',
            False,
        ),
    ],
)
def test_trace(body: bytes, expected: bool) -> None:
    assert r.trace_found(body, "svc") is expected


@pytest.mark.parametrize(
    ("mode", "result", "now", "expected"),
    [
        ("fresh", [], 200, False),
        ("fresh", [{"value": [0, "12"]}], 200, True),
        ("fresh", [{"value": [0, 12]}], 200, False),
        ("fresh", [{"value": [0, "13"]}], 200, False),
        ("fresh", [{"value": [0, "12"]}] * 2, 200, False),
        ("fresh", [{}], 200, False),
        ("fresh", [{"value": []}], 200, False),
        ("timestamp", [{"value": [0, "80"]}], 200, True),
        ("timestamp", [{"value": [0, "79.999"]}], 200, False),
        ("timestamp", [{"value": [0, "bad"]}], 200, False),
        ("stored", [{"values": [[0, "13"], [1, "12"]]}], 200, True),
        ("stored", [{"values": [[0, "13"]]}], 200, False),
        ("stored", [{"values": []}], 200, False),
    ],
)
def test_metrics(mode: str, result: object, now: int, expected: bool) -> None:
    body = json.dumps({"data": {"result": result}}).encode()
    if mode == "timestamp":
        assert r.metric_timestamp(body, now) is expected
    else:
        assert r.metric_found(body, 12, mode) is expected


def test_invalid_metrics() -> None:
    for body in (b"bad", b"null", b"[]", b'{"data":{"result":null}}'):
        assert not r.metric_found(body, 12, "fresh")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"bad", False),
        (b"null", False),
        (b"{}", False),
        (b'{"traces":{}}', False),
        (b'{"traces":[]}', False),
        (b'{"traces":[{}]}', True),
        (b'{"traces":[{},{}]}', True),
    ],
)
def test_grpc(body: bytes, expected: bool) -> None:
    assert r.grpc_found(body) is expected


def test_uri_reserved() -> None:
    path = r.query_path("fresh", "()*!'/", TRACE, 100, 200)
    assert path.endswith(
        "query=o11y_check_value%7Brun_id%3D%22%28%29%2A%21%27%2F%22%7D"
    )


def test_url_path_and_diagnostic() -> None:
    assert r.base_url("http://localhost:1/X") == "http://localhost:1/X"
    with pytest.raises(
        ValueError, match=r"^expected loopback HTTP URL with explicit port$"
    ):
        r.base_url("https://localhost:1")


def test_trace_optional_batches() -> None:
    resource = {
        "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
    }
    body = json.dumps(
        {"batches": [{}, {"resource": {}}, {"resource": resource}]}
    ).encode()
    assert r.trace_found(body, "svc")


def test_timestamp_zero_clock() -> None:
    body = b'{"data":{"result":[{"value":[0,"-120"]}]}}'
    assert r.metric_timestamp(body, 0)


@pytest.mark.parametrize("kind", ["fresh", "grpc", "log"])
def test_uri_slash(kind: str) -> None:
    path = r.query_path(kind, "/", TRACE, 100, 200)
    assert "%2F" in path
    assert '"/"' not in path


def test_invalid_query_kind() -> None:
    with pytest.raises(ValueError, match="not a valid QueryKind"):
        r.query_path("unknown", RUN, TRACE, 100, 200)
    assert not r.metric_found(b"{}", 1, "unknown")


def test_decode_constants() -> None:
    for body in (b"NaN", b"Infinity", b"-Infinity"):
        assert r.decode(body) == {}


def test_timestamp_shape_and_age() -> None:
    for body in (
        b"bad",
        b"{}",
        b"null",
        b'{"data":{"result":[]}}',
        b'{"data":{"result":[{"value":[]}]}}',
    ):
        assert not r.metric_timestamp(body, 1000)


def test_deep_json() -> None:
    body = b"[" * 100000 + b"0" + b"]" * 100000
    assert len(body) < 4 * 1024 * 1024
    assert r.decode(body) == {}
    assert r.log_classify(200, body, "fixture", 100) == "case=invalid-json"


@pytest.mark.parametrize("stamp", ["9" * 21, "9" * 5000, "sensitive", "\uff11\uff12"])
def test_invalid_timestamp(stamp: str) -> None:
    assert r.log_classify(200, response(stamp=stamp), "fixture", 100) == (
        "case=invalid-response-shape"
    )


@pytest.mark.parametrize("stamp", ["1", "9" * 20])
def test_timestamp_length_bounds(stamp: str) -> None:
    assert r.log_classify(200, response(stamp=stamp), "fixture", 0) == (
        "case=accepted count=1"
    )
