# cspell:ignore Brun
"""Receipt orchestration with replaceable HTTP, process, time and random seams."""

import io
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest
from tbhbops_cli import cli
from tbhbops_cli.o11y import receipt as command
from tbhbops_cli.o11y.filesystem import LocalFilesystem
from tbhbops_cli.o11y.http import BodyTooLargeError, TransportError
from tbhbops_cli.o11y.runner import Result, SubprocessRunner
lazy from tbhbops.o11y.validate import Document

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

URL = "http://127.0.0.1:12345"
RUN = "01" * 8
TRACE = "01" * 16
VALUE = 16843009
STATE = Path("/receipt-state")


@dataclass
class Clock:
    ticks: float = 0
    sleeps: list[float] = field(default_factory=list)
    random_calls: list[int] = field(default_factory=list)
    draws: list[bytes] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.ticks

    def epoch(self) -> float:
        return 1000 + self.ticks

    def sleep(self, delay: float) -> None:
        assert self.ticks < 200
        self.sleeps.append(delay)
        self.ticks += delay

    def random(self, size: int) -> bytes:
        assert len(self.random_calls) < 20
        self.random_calls.append(size)
        return self.draws.pop(0) if self.draws else b"\x01" * size


@dataclass
class Files(LocalFilesystem):
    text: str = f"{RUN} {TRACE} {VALUE} 1000\n"
    writes: list[tuple[Path, str | bytes, int]] = field(default_factory=list)
    denied: bool = False

    @override
    def read_text(self, path: Path) -> str:
        assert path == STATE
        if self.denied:
            msg = "sensitive"
            raise PermissionError(msg)
        return self.text

    @override
    def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None:
        if self.denied:
            msg = "sensitive"
            raise PermissionError(msg)
        self.writes.append((path, text, mode))


@dataclass
class Runner(SubprocessRunner):
    code: int | None = 0
    calls: list[tuple[list[str], dict[str, str], int]] = field(default_factory=list)

    @override
    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        assert input_data is None
        self.calls.append((list(argv), dict(env), timeout))
        return Result(self.code, "sensitive", "sensitive")


@dataclass
class Http:
    calls: list[tuple[str, str, bytes | None, dict[str, str], int]] = field(
        default_factory=list
    )
    overrides: dict[str, tuple[int, bytes] | Exception] = field(default_factory=dict)
    pending: int = 0

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: int,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, body, headers, timeout))
        for match, answer in self.overrides.items():
            if match in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        if method == "POST":
            return 200, b"x" * 37
        if self.pending:
            self.pending -= 1
            return 503, b"sensitive"
        data: Document
        if "/api/traces/" in url:
            data = {
                "batches": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": f"o11y-check-{RUN}"},
                                }
                            ]
                        }
                    }
                ]
            }
        elif "/loki/" in url:
            data = {
                "status": "success",
                "data": {
                    "result": [{"values": [["1000000000000", f"o11y-check {RUN}"]]}]
                },
            }
        elif "/api/search?" in url:
            data = {"traces": [{}]}
        elif "timestamp(" in url:
            data = {"data": {"result": [{"value": [0, "1000"]}]}}
        elif "query_range?" in url:
            data = {"data": {"result": [{"values": [[0, str(VALUE)]]}]}}
        else:
            data = {"data": {"result": [{"value": [0, str(VALUE)]}]}}
        return 200, json.dumps(data).encode()


@pytest.fixture
def system() -> cli.System:
    clock = Clock()
    return cli.System(
        {},
        lambda *_a: (200, b""),
        lambda *_a: "",
        Path("/"),
        Path("/words"),
        datetime(2026, 1, 1, tzinfo=UTC),
        "fixture",
        filesystem=Files(),
        runner=Runner(),
        receipt_http=Http(),
        monotonic=clock.monotonic,
        epoch=clock.epoch,
        sleep=clock.sleep,
        random_bytes=clock.random,
        egid=0,
        euid=0,
    )


def test_full(system: cli.System) -> None:
    lines, ok = command.receipt(
        system,
        URL,
        URL,
        state_file=STATE,
        grpc_argv=["telemetrygen", "traces"],
        docker_env={"PATH": "/bin", "DOCKER_HOST": "unix:///run/dind/docker.sock"},
    )
    assert ok
    assert lines == [
        f"ok receipt: trace {TRACE} found in Tempo with service o11y-check-{RUN}",
        "ok receipt: exactly one log line with the run's body found in Loki",
        f"ok receipt: one fresh sample equal to {VALUE} found in Mimir",
        f"ok receipt: gRPC trace found in Tempo by service o11y-check-grpc-{RUN}",
    ]
    assert isinstance(system.filesystem, Files)
    assert system.filesystem.writes == [(STATE, f"{RUN} {TRACE} {VALUE} 1000\n", 0o600)]
    assert isinstance(system.runner, Runner)
    assert system.runner.calls == [
        (
            [
                "telemetrygen",
                "traces",
                "--traces",
                "1",
                "--service",
                f"o11y-check-grpc-{RUN}",
            ],
            {"PATH": "/bin", "DOCKER_HOST": "unix:///run/dind/docker.sock"},
            120,
        )
    ]
    assert isinstance(system.receipt_http, Http)
    assert len(system.receipt_http.calls) == 8
    for method, url, body, headers, timeout in system.receipt_http.calls:
        assert timeout == 15
        assert url.startswith(URL)
        assert headers == (
            {"Content-Type": "application/json"} if method == "POST" else {}
        )
        assert (body is not None) == (method == "POST")


def test_retry_zero_and_value_mask(system: cli.System) -> None:
    clock = Clock(
        draws=[
            b"\x01" * 8,
            b"\0" * 16,
            b"\x01" * 16,
            b"\0" * 8,
            b"\x01" * 8,
            b"\xff" * 4,
        ]
    )
    system = replace(
        system,
        random_bytes=clock.random,
        receipt_http=Http(pending=1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    lines, ok = command.receipt(system, URL, URL, "traces", state_file=STATE)
    assert ok
    assert len(lines) == 1
    assert clock.random_calls == [8, 16, 16, 8, 8, 4]
    assert clock.sleeps == [2]
    assert isinstance(system.filesystem, Files)
    assert system.filesystem.writes == [
        (STATE, f"{RUN} {TRACE} 1073741823 1000\n", 0o600)
    ]


def test_recheck(system: cli.System) -> None:
    lines, ok = command.receipt(system, None, URL, recheck=STATE)
    assert ok
    assert lines == [f"ok receipt: run {RUN} still returns its traces, logs, metrics"]
    assert isinstance(system.receipt_http, Http)
    assert all(call[0] == "GET" for call in system.receipt_http.calls)
    assert len(system.receipt_http.calls) == 3
    assert isinstance(system.filesystem, Files)
    assert system.filesystem.writes == []


@pytest.mark.parametrize("recheck", [False, True])
def test_loki_summary(system: cli.System, recheck: bool) -> None:
    system = replace(
        system,
        receipt_http=Http(
            overrides={"/loki/": (200, b'{"status":"success","data":{"result":[]}}')}
        ),
    )
    lines, ok = command.receipt(
        system,
        None if recheck else URL,
        URL,
        "logs",
        recheck=STATE if recheck else None,
        state_file=None if recheck else STATE,
    )
    assert not ok
    floor = 0 if recheck else 940
    post = "no POST" if recheck else "post_http=200 post_bytes=37"
    assert lines[0] == (
        "FAIL receipt Loki: case=zero-lines count=0 query_http=200 "
        f"floor_s={floor} elapsed_s=60 {post}"
    )
    failure = (
        f"the log line of run {RUN} is no longer returned"
        if recheck
        else f"Loki did not return exactly the one log line of run {RUN}"
    )
    assert lines[1] == f"FAIL receipt: {failure}"
    assert isinstance(system.filesystem, Files)
    assert system.filesystem.writes == []


@pytest.mark.parametrize(
    ("signal", "match", "failure"),
    [
        ("traces", "/api/traces/", f"trace {TRACE} was not returned by Tempo"),
        (
            "metrics",
            "/mimir/",
            f"Mimir did not return one fresh sample equal to {VALUE} for run {RUN}",
        ),
    ],
)
@pytest.mark.parametrize("recheck", [False, True])
def test_failed_queries(
    system: cli.System, signal: str, match: str, failure: str, recheck: bool
) -> None:
    system = replace(system, receipt_http=Http(overrides={match: (302, b"sensitive")}))
    lines, ok = command.receipt(
        system, URL, URL, signal, recheck=STATE if recheck else None
    )
    if recheck:
        failure = (
            f"trace {TRACE} is no longer returned"
            if signal == "traces"
            else f"the metric sample of run {RUN} is no longer returned"
        )
    assert not ok
    assert lines == [f"FAIL receipt: {failure}"]


@pytest.mark.parametrize(
    ("answer", "case", "status"),
    [
        (
            TransportError("TimeoutError"),
            "transport-error transport=TimeoutError",
            "unavailable",
        ),
        (BodyTooLargeError(), "invalid-json", "200"),
        ((302, b"sensitive"), "http-error", "302"),
    ],
)
def test_http_outcomes(
    system: cli.System, answer: tuple[int, bytes] | Exception, case: str, status: str
) -> None:
    system = replace(system, receipt_http=Http(overrides={"/loki/": answer}))
    lines, ok = command.receipt(system, URL, URL, "logs")
    assert not ok
    assert lines[0] == (
        f"FAIL receipt Loki: case={case} query_http={status} "
        "floor_s=940 elapsed_s=60 post_http=200 post_bytes=37"
    )


@pytest.mark.parametrize(
    "answer", [(500, b"sensitive"), TransportError("OSError"), BodyTooLargeError()]
)
def test_failed_post(system: cli.System, answer: tuple[int, bytes] | Exception) -> None:
    system = replace(system, receipt_http=Http(overrides={"/v1/traces": answer}))
    lines, ok = command.receipt(system, URL, URL, state_file=STATE)
    assert not ok
    status = "500" if isinstance(answer, tuple) else "000"
    assert lines == [f"FAIL receipt: POST /v1/traces returned {status}"]


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        ("/api/traces/", [f"FAIL receipt: trace {TRACE} was not returned by Tempo"]),
        (
            "/mimir/",
            [
                (
                    f"FAIL receipt: Mimir did not return one fresh sample equal to "
                    f"{VALUE} for run {RUN}"
                )
            ],
        ),
        (
            "timestamp(",
            [
                (
                    f"FAIL receipt: Mimir did not return one fresh sample equal to "
                    f"{VALUE} for run {RUN}"
                )
            ],
        ),
        (
            "/api/search?",
            [
                (
                    f"ok receipt: trace {TRACE} found in Tempo "
                    f"with service o11y-check-{RUN}"
                ),
                f"FAIL receipt: no trace for service o11y-check-grpc-{RUN} was found",
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "answer", [(200, b"bad"), TransportError("OSError"), (500, b"sensitive")]
)
def test_classifier_failures(
    system: cli.System,
    match: str,
    expected: list[str],
    answer: tuple[int, bytes] | Exception,
) -> None:
    system = replace(system, receipt_http=Http(overrides={match: answer}))
    signal = "metrics" if match in {"/mimir/", "timestamp("} else "traces"
    lines, ok = command.receipt(
        system,
        URL,
        URL,
        signal,
        grpc_argv=["telemetrygen"] if match == "/api/search?" else None,
    )
    assert not ok
    assert lines == expected


@pytest.mark.parametrize("code", [1, None])
def test_grpc_runner_failure(system: cli.System, code: int | None) -> None:
    system = replace(system, runner=Runner(code=code))
    lines, ok = command.receipt(system, URL, URL, "traces", grpc_argv=["telemetrygen"])
    assert not ok
    assert lines[-1] == "FAIL receipt: telemetrygen could not send a trace over gRPC"


def test_grpc_deadline(system: cli.System) -> None:
    clock = Clock()
    system = replace(
        system,
        receipt_http=Http(overrides={"/api/search?": (200, b"{}")}),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    lines, ok = command.receipt(system, URL, URL, "traces", grpc_argv=["telemetrygen"])
    assert not ok
    assert (
        lines[-1]
        == f"FAIL receipt: no trace for service o11y-check-grpc-{RUN} was found"
    )
    assert clock.ticks == 120
    assert clock.sleeps == [2] * 60


@pytest.mark.parametrize("reading", [True, False])
def test_filesystem_failure(system: cli.System, reading: bool) -> None:
    system = replace(system, filesystem=Files(denied=True))
    lines, ok = command.receipt(
        system,
        URL,
        URL,
        "traces",
        recheck=STATE if reading else None,
        state_file=None if reading else STATE,
    )
    assert not ok
    assert lines[-1] == "FAIL receipt: PermissionError"


def test_bad_state(system: cli.System) -> None:
    system = replace(system, filesystem=Files(text="sensitive"))
    assert command.receipt(system, None, URL, recheck=STATE) == (
        ["FAIL receipt: ValueError"],
        False,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"otlp_url": None},
        {"signals": ""},
        {"signals": "trace"},
        {"signals": "logs,"},
        {"state_file": STATE, "recheck": STATE},
        {"grpc_argv": []},
        {"grpc_argv": ["x"], "recheck": STATE},
        {"grpc_argv": ["x"], "signals": "logs"},
        {"grafana_url": "https://localhost:123"},
        {"otlp_url": "http://example.test:123"},
    ],
)
def test_usage(system: cli.System, kwargs: dict[str, object]) -> None:
    args = {"otlp_url": URL, "grafana_url": URL, **kwargs}
    with pytest.raises(
        ValueError, match=r"required|invalid|exclusive|requires|loopback"
    ):
        command.receipt(system, **args)  # pyrefly: ignore[bad-argument-type]
    assert isinstance(system.receipt_http, Http)
    assert system.receipt_http.calls == []


@pytest.mark.parametrize(
    ("tail", "code", "expected"),
    [
        (
            ["--signals", "traces"],
            0,
            [f"ok receipt: trace {TRACE} found in Tempo with service o11y-check-{RUN}"],
        ),
        (["--signals", "bad"], 2, ["tbhbops o11y receipt: invalid signals"]),
    ],
)
def test_cli(
    system: cli.System, tail: list[str], code: int, expected: list[str]
) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert (
        cli.run(
            ["o11y", "receipt", "--otlp-url", URL, "--grafana-url", URL, *tail],
            system,
            out,
            err,
        )
        == code
    )
    assert out.getvalue().splitlines() == expected
    assert err.getvalue() == ""


def test_cli_failure_and_recheck(system: cli.System) -> None:
    out = io.StringIO()
    assert (
        cli.run(
            ["o11y", "receipt", "--grafana-url", URL, "--recheck", str(STATE)],
            system,
            out,
            io.StringIO(),
        )
        == 0
    )
    system = replace(
        system, receipt_http=Http(overrides={"/v1/traces": (500, b"sensitive")})
    )
    out = io.StringIO()
    assert (
        cli.run(
            ["o11y", "receipt", "--otlp-url", URL, "--grafana-url", URL],
            system,
            out,
            io.StringIO(),
        )
        == 1
    )
    assert out.getvalue() == "FAIL receipt: POST /v1/traces returned 500\n"


def test_sent_ids_and_queries(system: cli.System) -> None:
    _, ok = command.receipt(system, URL, URL, state_file=STATE)
    assert ok
    assert isinstance(system.receipt_http, Http)
    calls = system.receipt_http.calls
    trace = json.loads(calls[0][2] or b"")
    span = trace["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == TRACE
    assert span["spanId"] == "01" * 8
    assert calls[3][1] == f"{URL}/api/datasources/proxy/uid/tempo/api/traces/{TRACE}"
    assert f"o11y-check-{RUN}%22%7D&start=940000000000&end=1060000000000" in calls[4][1]
    assert calls[5][1].endswith(f"query=o11y_check_value%7Brun_id%3D%22{RUN}%22%7D")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BodyTooLargeError(), (200, b"", "")),
        (TransportError("URLError"), (0, b"", "URLError")),
    ],
)
def test_empty_outcome(
    system: cli.System, error: Exception, expected: tuple[int, bytes, str]
) -> None:
    system = replace(system, receipt_http=Http(overrides={"query": error}))
    check = command.Check(system, URL, RUN, TRACE, VALUE, 1000, rechecking=False)
    assert check.request("GET", URL + "/query") == expected


def test_recheck_uses_range(system: cli.System) -> None:
    assert command.receipt(system, None, URL, "metrics", recheck=STATE)[1]
    assert isinstance(system.receipt_http, Http)
    assert system.receipt_http.calls[0][1] == (
        f"{URL}/api/datasources/proxy/uid/mimir/api/v1/query_range?"
        f"query=o11y_check_value%7Brun_id%3D%22{RUN}%22%7D&start=940&end=1000&step=15"
    )


@pytest.mark.parametrize(("status", "value"), [(200, "wrong"), (302, str(VALUE))])
def test_metric_value_before_timestamp(
    system: cli.System, status: int, value: str
) -> None:
    body = json.dumps({"data": {"result": [{"value": [0, value]}]}}).encode()
    system = replace(
        system, receipt_http=Http(overrides={"/query?query=o11y": (status, body)})
    )
    assert not command.receipt(system, URL, URL, "metrics")[1]


def test_stale_timestamp(system: cli.System) -> None:
    system = replace(
        system,
        receipt_http=Http(
            overrides={
                "timestamp(": (200, b'{"data":{"result":[{"value":[0,"879"]}]}}')
            }
        ),
    )
    assert not command.receipt(system, URL, URL, "metrics")[1]


def test_elapsed_nonzero_clock(system: cli.System) -> None:
    clock = Clock(ticks=10)
    system = replace(
        system,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        receipt_http=Http(overrides={"/loki/": (500, b"sensitive")}),
    )
    check = command.Check(system, URL, RUN, TRACE, VALUE, 1000, rechecking=False)
    lines: list[str] = []
    assert not check.log_receipt(lines)
    assert "elapsed_s=60 no POST" in lines[0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"signals": "bad"}, "invalid signals"),
        (
            {"state_file": STATE, "recheck": STATE},
            "state-file and recheck are mutually exclusive",
        ),
        ({"otlp_url": None}, "otlp-url is required"),
        ({"grpc_argv": []}, "grpc requires an argv and traces"),
    ],
)
def test_exact_usage(
    system: cli.System, kwargs: dict[str, object], message: str
) -> None:
    args = {"otlp_url": URL, "grafana_url": URL, **kwargs}
    with pytest.raises(ValueError, match=r".+") as exc:
        command.receipt(system, **args)  # pyrefly: ignore[bad-argument-type]
    assert str(exc.value) == message


def test_cli_writes_state(system: cli.System) -> None:
    out = io.StringIO()
    assert (
        cli.run(
            [
                "o11y",
                "receipt",
                "--otlp-url",
                URL,
                "--grafana-url",
                URL,
                "--state-file",
                str(STATE),
                "--signals",
                "traces",
            ],
            system,
            out,
            io.StringIO(),
        )
        == 0
    )
    assert isinstance(system.filesystem, Files)
    assert system.filesystem.writes == [(STATE, f"{RUN} {TRACE} {VALUE} 1000\n", 0o600)]


def test_receipt_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["o11y", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "send and query an exact receipt" in help_text
    assert "XX" not in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["o11y", "receipt"],
        [
            "o11y",
            "receipt",
            "--grafana-url",
            URL,
            "--state-file",
            "a",
            "--recheck",
            "b",
        ],
    ],
)
def test_parser_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("stamp", ["9" * 21, "sensitive-timestamp"])
def test_invalid_timestamp_output(system: cli.System, stamp: str) -> None:
    body = json.dumps(
        {
            "status": "success",
            "data": {"result": [{"values": [[stamp, f"o11y-check {RUN}"]]}]},
        }
    ).encode()
    system = replace(system, receipt_http=Http(overrides={"/loki/": (200, body)}))
    lines, ok = command.receipt(system, URL, URL, "logs")
    assert not ok
    assert lines == [
        (
            "FAIL receipt Loki: case=invalid-response-shape query_http=200 "
            "floor_s=940 elapsed_s=60 post_http=200 post_bytes=37"
        ),
        f"FAIL receipt: Loki did not return exactly the one log line of run {RUN}",
    ]
    assert all(stamp not in line for line in lines)


def test_cli_recheck_state_file(
    system: cli.System, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.run(
            [
                "o11y",
                "receipt",
                "--grafana-url",
                URL,
                "--recheck",
                str(STATE),
                "--state-file",
                str(STATE),
            ],
            system,
            io.StringIO(),
            io.StringIO(),
        )
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines()[-1] == (
        "tbhbops o11y receipt: error: argument --state-file: "
        "not allowed with argument --recheck"
    )
