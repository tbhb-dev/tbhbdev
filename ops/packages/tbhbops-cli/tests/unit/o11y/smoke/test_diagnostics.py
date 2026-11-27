"""Smoke evidence handling and cleanup."""

# cspell:ignore udcff udcfe

import io
from dataclasses import replace
from pathlib import Path
lazy from collections.abc import Mapping, Sequence

import pytest
from tbhbops.o11y import smoke
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    bootstrap,
    diagnostics,
    readiness,
    runtime,
    safety,
)
from tbhbops_cli.o11y.smoke import setup as setup_stage
from unit.o11y.smoke.runtime_fixture import (
    ARTIFACTS,
    HOST,
    OUT,
    interrupt,
    make_system,
    prepared,
    run,
)
lazy from tbhbops_cli.o11y.filesystem import Filesystem


@pytest.mark.usefixtures("early_stages")
def test_failure_capture_clean_bounded() -> None:
    fs, engine, system = make_system()
    engine.fail_match = "up -d"
    code, output = run(system)
    assert code == 1
    assert "FAIL 2: up failed" in output
    assert "untrusted" not in output
    assert "clean Alloy and Loki logs retained" in output
    assert [
        (argv[-3:], timeout, byte_limit, line_limit)
        for argv, timeout, byte_limit, line_limit in engine.head_calls
    ] == [(["logs", "--no-color", "loki"], 120, 4 * 1024 * 1024, 200)]
    assert fs.list_dir(ARTIFACTS) == ("alloy.log", "loki-head.log", "loki.log")
    for name in fs.list_dir(ARTIFACTS):
        content = fs.read_text(ARTIFACTS / name)
        assert len(content.splitlines()) == 200
        assert fs.modes[ARTIFACTS / name] & 0o777 == 0o600
    assert fs.modes[ARTIFACTS] & 0o777 == 0o700
    assert fs.read_text(ARTIFACTS / "loki-head.log") == "".join(
        f"{n}\n" for n in range(1, 201)
    )
    assert fs.read_text(ARTIFACTS / "loki.log") == "".join(
        f"{n}\n" for n in range(4801, 5001)
    )
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    assert ("mkdir", OUT / "failure-logs") in fs.calls
    assert ("chmod", OUT / "failure-logs", 0o700) in fs.calls
    for name in ("alloy", "loki", "loki-head"):
        assert ("write", OUT / "failure-logs" / (name + ".log"), 0o600) in fs.calls


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("name", ["loki", "loki-head"])
def test_contaminated_capture_withheld(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = make_system()
    engine.fail_match = "up -d"
    engine.contaminated = name
    # Head contamination must be inside the first 200 lines.
    original = engine.run

    def contaminate(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        head = "logs" in argv and argv[-1] == "loki" and "--tail" not in argv
        if name == "loki-head" and head:
            return Result(
                0, fs.read_text(HOST / "secrets/rustfs.secret_key") + result.stdout
            )
        return result

    monkeypatch.setattr(engine, "run", contaminate)
    code, output = run(system)
    assert code == 1
    assert "no logs promoted" in output
    assert fs.list_dir(ARTIFACTS) == ()
    assert "C" * 40 not in output
    assert fs.lstat(OUT) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    "fail", ["logs --no-color --tail 200 loki", "logs --no-color loki"]
)
def test_failed_capture_promotes_nothing(
    fail: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = make_system()
    original = engine.run

    def failure(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        return (
            Result(17, "untrusted", "untrusted")
            if fail in " ".join(argv) or "up" in argv
            else result
        )

    monkeypatch.setattr(engine, "run", failure)
    code, output = run(system)
    assert code == 1
    assert "no logs promoted" in output
    assert "untrusted" not in output
    assert fs.list_dir(ARTIFACTS) == ()
    assert fs.lstat(OUT) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("exc", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("name", ["alloy.log", "loki-head.log"])
def test_failed_or_interrupted_scan_preserves_status(
    exc: type[BaseException], name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = make_system()
    engine.fail_match = "up -d"
    original = diagnostics.scan_path

    def failure(filesystem: Filesystem, path: Path, needles: tuple[bytes, ...]) -> None:
        if path.name == name:
            if exc is KeyboardInterrupt:
                interrupt()
            raise exc
        original(filesystem, path, needles)

    monkeypatch.setattr(diagnostics, "scan_path", failure)
    code, output = run(system)
    assert code == 1
    assert "no logs promoted" in output
    assert fs.lstat(OUT) is None
    assert fs.list_dir(ARTIFACTS) == ()


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("failure", ["transcript", "cleanup"])
def test_finalization_failure_preserves_original(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, system = make_system()
    engine.fail_match = "up -d"
    if failure == "transcript":
        fs.denied_calls.add(("open", OUT / "transcript.log"))
        original = fs.write_atomic

        def write(path: Path, text: str | bytes, mode: int) -> None:
            if path.name == "transcript.log":
                raise OSError
            original(path, text, mode)

        monkeypatch.setattr(fs, "write_atomic", write)
    else:

        def cleanup(_self: runtime.Smoke) -> bool:
            return False

        monkeypatch.setattr(diagnostics, "cleanup", cleanup)
    code, output = run(system)
    assert code == 1
    assert (
        "transcript finalization failed"
        if failure == "transcript"
        else "cleanup failed"
    ) in output


@pytest.mark.usefixtures("early_stages")
def test_diagnostics_disk_unavailable_and_pressure() -> None:
    for disk, suffix in (
        (Result(14, "private", "private"), "unavailable"),
        (
            Result(0, "1000 50"),
            "ratio=0.950000 at or above 0.90 (pressure at collection time)",
        ),
    ):
        _, engine, system = make_system()
        engine.fail_match = "up -d"
        engine.disk = disk
        code, output = run(system)
        assert code == 1
        assert "smoke diagnostics: Loki disk usage " + suffix in output
        assert "inconclusive (no transition in head and tail windows)" in output
        assert "private" not in output


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("phrase", [smoke.THROTTLE, smoke.RESUME])
def test_diagnostics_transitions(phrase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, engine, system = make_system()
    engine.fail_match = "up -d"
    original = engine.run

    def transition(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        return (
            Result(0, phrase + ' label="private"\n' + result.stdout)
            if "logs" in argv and argv[-1] == "loki"
            else result
        )

    monkeypatch.setattr(engine, "run", transition)
    code, output = run(system)
    assert code == 1
    assert phrase in output
    assert 'label="private"' not in output
    assert ("throttling confirmed" in output) == (phrase == smoke.THROTTLE)


def test_finalized_transcript_idempotent_and_artifact_scan() -> None:
    fs, _, instance = prepared()
    instance.emit("last stage output")
    instance.finalize_transcript()
    before = list(fs.calls)
    instance.finalize_transcript()
    assert fs.calls == before
    assert fs.read_text(OUT / "transcript.log") == "smoke setup\nlast stage output\n"
    fs.mkdir(ARTIFACTS)
    diagnostics.scans(instance)
    fs.write_atomic(ARTIFACTS / "existing", "clean", 0o600)
    fs.write_atomic(OUT / "empty.log", "", 0o600)
    diagnostics.scans(instance)
    fs.write_atomic(ARTIFACTS / "existing", instance.sentinels.token, 0o600)
    with pytest.raises(safety.SmokeError, match="sentinel secret: existing"):
        diagnostics.scans(instance)


@pytest.mark.parametrize("error", ["result", "exception"])
def test_cleanup_attempts_every_step(
    error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, instance = prepared()
    if error == "result":
        engine.fail_match = "down -v --remove-orphans"
    else:

        def fail(*_args: object, **_kwargs: object) -> Result:
            raise OSError

        monkeypatch.setattr(instance, "compose", fail)
    assert diagnostics.cleanup(instance) is False
    assert fs.lstat(HOST) is None
    assert fs.lstat(OUT) is None
    removals = [argv[-1] for argv, _, _ in engine.calls if argv[1:3] == ["rm", "-f"]]
    assert removals == [instance.project + "-" + name for name in diagnostics.FIXTURES]


@pytest.mark.usefixtures("early_stages")
def test_capture_stderr_scanned() -> None:
    fs, engine, system = make_system()
    engine.fail_match = "up -d"
    engine.log_error = "diagnostic stderr"
    code, output = run(system)
    assert code == 1
    assert "clean Alloy" in output
    assert fs.list_dir(ARTIFACTS) == ("alloy.log", "loki-head.log", "loki.log")


@pytest.mark.usefixtures("early_stages")
def test_original_status_survives_capture_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, system = make_system()

    def fail(_self: runtime.Smoke) -> None:
        rule = "fixture failure"
        raise safety.SmokeError(rule, 23)

    monkeypatch.setattr(readiness, "group3", fail)
    code, output = run(system)
    assert code == 23
    assert "clean Alloy and Loki logs retained" in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


@pytest.mark.usefixtures("early_stages")
def test_failed_up_sentinel_names_file() -> None:
    fs, engine, system = make_system()
    engine.fail_match = "up -d"
    engine.replacement = Result(17, "C" * 40)
    code, output = run(system)
    assert code == 1
    assert "sentinel secret: up.log" in output
    assert "C" * 40 not in output
    assert fs.list_dir(ARTIFACTS) == ()


def test_raw_disk_failure_status_and_binary_logs() -> None:
    fs, engine, instance = prepared()
    engine.disk = Result(17, "100 7")
    assert instance.disk_counts() == ""
    engine.fail_match = "ps -q"
    assert instance.inspect("loki", "template") == Result(1)
    engine.fail_match = "up -d"
    engine.replacement = Result(17, "\udcff", "\udcfe")
    assert instance.logged(["up", "-d"], "raw.log") == engine.replacement
    assert fs.read_bytes(OUT / "raw.log") == b"\xff\xfe"


def test_log_names_and_capture_error_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, instance = prepared()
    setup_stage.group1(instance)
    bootstrap.group2(instance)
    readiness.group3a(instance)
    assert {name for name in fs.list_dir(OUT) if name.endswith(".log")} == {
        "up.log",
        "stubs.log",
        "wait.log",
        "readiness-unhealthy.log",
        "readiness-failed.log",
    }
    engine.log_error = "\udcff"
    diagnostics.capture_failure_logs(instance)
    assert fs.read_bytes(OUT / "failure-logs/capture.err") == b"\xff" * 3
    assert fs.modes[OUT / "failure-logs/capture.err"] & 0o777 == 0o600
    assert fs.modes[OUT / "failure-logs"] & 0o777 == 0o700
    engine.log_error = ""
    # Stderr from the first service must survive later empty captures and be scanned.
    original = engine.run

    def dirty(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        if "logs" in argv and argv[-1] == "alloy":
            return replace(result, stderr=instance.sentinels.token.decode())
        return result

    monkeypatch.setattr(engine, "run", dirty)
    with pytest.raises(safety.SmokeError) as failure:
        diagnostics.capture_failure_logs(instance)
    assert str(failure.value) == "sentinel secret: capture.err"


def test_finish_failures_always_keep_status_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # All output-only branches are exercised directly as well as through dispatch.
    for kind in ("clean", "capture", "scan", "evidence", "finalize", "cleanup"):
        fs, _, instance = prepared()
        setup_stage.group1(instance)
        instance.started = True
        with monkeypatch.context() as patch:
            if kind == "capture":

                def capture(_run: runtime.Smoke) -> bytes:
                    rule = "diagnostics: capture failed; no logs promoted"
                    raise safety.SmokeError(rule)

                patch.setattr(diagnostics, "capture_failure_logs", capture)
            elif kind == "scan":

                def scan_failure(_run: runtime.Smoke) -> bytes:
                    raise OSError

                patch.setattr(diagnostics, "capture_failure_logs", scan_failure)
            elif kind == "evidence":

                def counts() -> str:
                    raise OSError

                patch.setattr(instance, "disk_counts", counts)
            elif kind == "finalize":

                def finalize() -> None:
                    raise OSError

                patch.setattr(instance, "finalize_transcript", finalize)
            elif kind == "cleanup":

                def cleanup(_run: runtime.Smoke) -> bool:
                    return False

                patch.setattr(diagnostics, "cleanup", cleanup)
            output = io.StringIO()
            diagnostics.finish(instance, 23, output)
        text = output.getvalue()
        expected = {
            "capture": (
                "smoke diagnostics: diagnostics: capture failed; "
                "no logs promoted; no logs promoted"
            ),
            "scan": "smoke diagnostics: capture or scan failed; no logs promoted",
            "evidence": "smoke diagnostics: disk evidence unavailable",
            "finalize": "smoke diagnostics: transcript finalization failed",
            "cleanup": "smoke diagnostics: cleanup failed",
        }
        if kind != "clean":
            assert expected[kind] in text.splitlines()
        if kind != "evidence":
            assert (
                "smoke diagnostics: Loki disk usage ratio=0.800000 below 0.90"
                in text.splitlines()
            )
            assert (
                "smoke diagnostics: Loki WAL throttling inconclusive "
                "(no transition in head and tail windows)" in text.splitlines()
            )
        if kind != "cleanup":
            assert fs.lstat(OUT) is None
            assert fs.lstat(HOST) is None


def test_capture_failure_rule_and_binary_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, instance = prepared()
    setup_stage.group1(instance)
    engine.fail_match = "logs --no-color --tail 200 alloy"
    engine.replacement = Result(17, "private")
    with pytest.raises(safety.SmokeError) as failure:
        diagnostics.capture_failure_logs(instance)
    assert str(failure.value) == "diagnostics: capture failed; no logs promoted"
    engine.fail_match = ""
    original = engine.run

    def binary(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        return (
            replace(result, stdout="\udcff" + result.stdout)
            if "logs" in argv
            else result
        )

    monkeypatch.setattr(engine, "run", binary)
    diagnostics.capture_failure_logs(instance)
    assert fs.read_bytes(ARTIFACTS / "loki-head.log").startswith(b"\xff1\n")


def test_already_removed_fixture_names_are_clean() -> None:
    fs, engine, instance = prepared()
    engine.fail_match = "docker rm -f"
    assert diagnostics.cleanup(instance) is True
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_remove_data_preserves_mount_root_and_siblings(platform: str) -> None:
    fs, engine, instance = prepared()
    if platform == "darwin":
        instance = replace(
            instance,
            system=replace(instance.system, platform=platform),
            data=Path("/var/lib/o11y-test/smoke-01010101"),
        )
        fs.mkdir(instance.data)
    fs.write_atomic(instance.data / "unexpected", "remove", 0o600)
    sibling = instance.data.parent / "sibling"
    fs.write_atomic(sibling, "keep", 0o600)
    names = fs.list_dir(instance.data)
    assert diagnostics.remove_data(instance) == Result(0)
    assert fs.read_text(sibling) == "keep"
    if platform == "linux":
        assert fs.lstat(instance.data) is not None
        assert fs.list_dir(instance.data) == ()
        assert engine.calls[-1][0][-len(names) :] == ["/d/" + name for name in names]
    else:
        assert fs.lstat(instance.data) is None
        assert engine.calls[-1][0][-1] == "/d/smoke-01010101"
