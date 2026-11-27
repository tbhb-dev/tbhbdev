import io
import json
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock
from urllib.request import Request
lazy from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backup/status.py"


@pytest.fixture
def status() -> ModuleType:
    spec = spec_from_file_location("ca_backup_status", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_record_preserves_last_success(
    status: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "monitor-start"
    baseline.touch()
    os.utime(baseline, (10, 10))
    monkeypatch.setattr(sys, "argv", ["status", "record", "--state", str(tmp_path)])
    assert status.read_status(tmp_path) == (0, 10)
    monkeypatch.setenv("SERVICE_RESULT", "exit-code")
    status.main()
    assert status.read_status(tmp_path) == (1, 10)
    monkeypatch.setenv("SERVICE_RESULT", "success")
    monkeypatch.setattr(status.time, "time", Mock(return_value=100))
    status.main()
    assert status.read_status(tmp_path) == (0, 100)
    monkeypatch.setenv("SERVICE_RESULT", "timeout")
    status.main()
    assert status.read_status(tmp_path) == (1, 100)
    assert not (tmp_path / "status.pending").exists()


@pytest.mark.parametrize(
    "reply", [b"{}", b'{"partialSuccess": {"rejectedDataPoints": "2"}}']
)
def test_publish(
    status: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: bytes
) -> None:
    (tmp_path / "status.json").write_text("[1, 100]")
    endpoint = "http://otlp.example.test:4318/v1/metrics"
    monkeypatch.setattr(
        sys,
        "argv",
        ["status", "publish", "--state", str(tmp_path), "--endpoint", endpoint],
    )
    monkeypatch.setattr(status.time, "time_ns", Mock(return_value=101_000_000_000))
    opener = Mock()
    opener.open.return_value = io.BytesIO(reply)
    monkeypatch.setattr(status, "build_opener", Mock(return_value=opener))
    if reply == b"{}":
        status.main()
    else:
        with pytest.raises(RuntimeError, match="collector rejected"):
            status.main()
    request: object = opener.open.call_args.args[0]
    assert isinstance(request, Request)
    assert request.get_method() == "POST"
    assert request.full_url == endpoint
    assert isinstance(request.data, bytes)
    scope = json.loads(request.data)["resourceMetrics"][0]["scopeMetrics"][0]
    assert [metric["name"] for metric in scope["metrics"]] == [
        status.FAILED,
        status.REFERENCE,
    ]
    points = [metric["gauge"]["dataPoints"][0] for metric in scope["metrics"]]
    assert [point["asInt"] for point in points] == ["1", "100"]
    for point in points:
        assert point["timeUnixNano"] == "101000000000"
        assert point["attributes"] == [
            {"key": "host", "value": {"stringValue": "nas-a"}}
        ]


def test_reject_non_http_endpoint(status: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must use HTTP or HTTPS"):
        status.publish(tmp_path, "file:///tmp/sink")
