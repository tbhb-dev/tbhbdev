"""Smoke stage command, HTTP, artifact, and failure contracts."""

import hashlib
import json
from pathlib import Path
lazy from collections.abc import Callable

import pytest
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    alerts,
    exports,
    grafana,
    lifecycle,
    monitoring,
    runtime,
    safety,
    telemetry,
)
from tbhbops_cli.o11y.smoke import capacity as cap
from test_o11y_cli import Fake
from unit.o11y.smoke.grafana_fixture import instance_b
from unit.o11y.smoke.metrics_fixture import HttpC
from unit.o11y.smoke.runtime_fixture import OUT
from unit.o11y.smoke.stack_fixture import SOURCE, AdminC, EngineC, instance_c
lazy from tbhbops.o11y.validate import Document

TESTDATA = Path(__file__).parents[3] / "testdata"


def export_contract(stage: str) -> Document:
    _, engine, http, run = instance_b()
    if stage == "group6":
        exports.group5(run)
    before_commands, before_http, before_lines = (
        len(engine.calls),
        len(http.calls),
        len(run.transcript),
    )
    getattr(grafana if stage == "group4" else exports, stage)(run)
    return {
        "commands": [
            [argv, timeout] for argv, _, timeout in engine.calls[before_commands:]
        ],
        "http": [
            [method, url, body.decode() if body is not None else None, headers, timeout]
            for method, url, body, headers, timeout in http.calls[before_http:]
        ],
        "transcript": run.transcript[before_lines:],
    }


def stage_run(stage: str) -> tuple[Fake, EngineC, HttpC, runtime.Smoke]:
    fs, engine, http, run = instance_c()
    if stage == "group10":
        lifecycle.group8(run, SOURCE)
    if stage in {"group13", "cleanup"}:
        http.email_off = True
    if stage == "cleanup":
        lifecycle.group13(run, SOURCE)
    return fs, engine, http, run


STAGES: dict[str, Callable[..., None]] = {
    "smoke": cap.smoke,
    "group7": monitoring.group7,
    "group8": lifecycle.group8,
    "group9": lifecycle.group9,
    "group10": lifecycle.group10,
    "group11": telemetry.group11,
    "group12": alerts.group12,
    "group13": lifecycle.group13,
    "cleanup": cap.cleanup,
}


def invoke(stage: str, run: runtime.Smoke) -> None:
    if stage in {"smoke", "group7", "group8", "group12", "group13"}:
        STAGES[stage](run, SOURCE)
    else:
        STAGES[stage](run)


def stage_contract(stage: str) -> Document:
    fs, engine, http, run = stage_run(stage)
    count = len(run.transcript)
    engine.calls.clear()
    http.calls.clear()
    fs.calls.clear()
    invoke(stage, run)
    assert isinstance(run.system.admin_http, AdminC)
    return {
        "commands": [[argv, timeout] for argv, _, timeout in engine.calls],
        "http": [
            [method, url, body.decode() if body is not None else None, headers, timeout]
            for method, url, body, headers, timeout in http.calls
        ],
        "uploads": engine.uploaded,
        "transcript": run.transcript[count:],
        "admin": [
            [url, timeout, method]
            for url, _, timeout, method in run.system.admin_http.calls
        ],
        "writes": {
            str(p): [mode, hashlib.sha256(fs.read_bytes(p)).hexdigest()]
            for op, p, mode in (call for call in fs.calls if call[0] == "write")
            if p in fs.files
        },
        "files": {
            str(p.relative_to(OUT)): fs.read_text(p)
            for p in sorted(fs.files)
            if OUT in p.parents
            and p.name
            in {"agents.yaml", "rules.yaml", "processes.log", "transcript.log"}
        },
    }


def fault_cases(kind: str, stages: tuple[str, ...]) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for stage in stages:
        path = TESTDATA / f"smoke-c-{stage}.json"
        if path.exists():
            saved = json.loads(path.read_text())
            texts: set[str] = {
                " ".join(call[0]) if kind == "commands" else call[1]
                for call in saved[kind]
            }
            cases.extend((stage, text) for text in sorted(texts))
    return cases


def check_export_protocol(stage: str) -> None:
    expected = json.loads((TESTDATA / f"smoke-b-{stage}.json").read_text())
    assert export_contract(stage) == expected


def check_stage_protocol(stage: str) -> None:
    expected = json.loads((TESTDATA / f"smoke-c-{stage}.json").read_text())
    assert stage_contract(stage) == expected


def check_process_failure(stage: str, match: str) -> None:
    _, engine, _, run = stage_run(stage)
    engine.overrides[match] = Result(17, "private-body", "private-error")
    check_failure(stage, run, f"commands|{stage}|{match}")


def check_http_failure(stage: str, match: str) -> None:
    _, _, http, run = stage_run(stage)
    http.overrides[match] = (500, b"private-body")
    check_failure(stage, run, f"http|{stage}|{match}")


def check_failure(stage: str, run: runtime.Smoke, key: str) -> None:
    with pytest.raises(safety.SmokeError) as failure:
        invoke(stage, run)
    assert "private" not in str(failure.value)
    failures = json.loads((TESTDATA / "smoke-c-failures.json").read_text())
    assert str(failure.value) == failures[key]
    assert "private" not in "".join(run.transcript)
