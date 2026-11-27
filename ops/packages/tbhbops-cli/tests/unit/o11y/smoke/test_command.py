"""Smoke command dispatch, stage order, and failure reporting."""

import argparse
import errno
import io
import json
import signal
import stat
from dataclasses import replace
from pathlib import Path
from typing import NoReturn
lazy from collections.abc import Callable, Mapping, Sequence

import pytest
from tbhbops.o11y import init, smoke
from tbhbops_cli import cli
from tbhbops_cli.common import CliError
from tbhbops_cli.o11y import init as initialization
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    alerts,
    bootstrap,
    command,
    diagnostics,
    exports,
    grafana,
    lifecycle,
    monitoring,
    readiness,
    runtime,
    safety,
    telemetry,
)
from tbhbops_cli.o11y.smoke import capacity as cap
from tbhbops_cli.o11y.smoke import setup as setup_stage
from test_o11y_cli import SOURCE
from unit.o11y.smoke.grafana_fixture import (
    Http,
    execute,
    setup_b,
)
from unit.o11y.smoke.runtime_fixture import (
    ARTIFACTS,
    HOST,
    OUT,
    Ports,
    interrupt,
    make_system,
    run,
)
from unit.o11y.smoke.stack_fixture import SOURCE as STACK_SOURCE
from unit.o11y.smoke.stack_fixture import instance_c


@pytest.mark.usefixtures("early_stages")
def test_success_and_environment() -> None:
    fs, engine, system = make_system()
    code, output = run(system)
    assert code == 0, output
    assert (
        output == "engine mode: local\nsmoke setup\n"
        f"ok 1: {smoke.SUCCESS['1']}\n"
        "smoke Loki disk usage (started): 0.800000\n"
        f"ok 2: {smoke.SUCCESS['2']}\n"
        f"ok 3: {smoke.SUCCESS['3']}\n"
        "smoke Loki WAL threshold: 0.99\n"
        f"ok 3a: {smoke.SUCCESS['3a']}\n"
    )
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    assert fs.list_dir(ARTIFACTS) == ()
    assert "secret-value" not in output
    assert isinstance(system.ports, Ports)
    assert system.ports.calls == [13000, 14317, 14318]
    for argv, env, timeout in engine.calls:
        if "--wait" in argv:
            assert timeout == (60 if argv[-1] == "30" else 210)
        elif "up" in argv:
            assert timeout == 600
        assert "secret-value" not in str(argv)
        assert "DOCKER_CONTEXT" not in env
        assert "DOCKER_HOST" not in env
        assert not any(key.startswith("COMPOSE_") for key in env)
        assert {key: env[key] for key in smoke.FIXED_ENV} == smoke.FIXED_ENV
        assert env["O11Y_DATA_DIR"] == str(HOST)
        assert env["O11Y_SECRETS_OWNER"] == "123:456"
        if argv[1] == "compose":
            assert argv[argv.index("-p") + 1] == "o11y-smoke-01010101"
        if "exec" in argv:
            assert argv[argv.index("exec") : argv.index("curl") + 1] == [
                "exec",
                "-T",
                "rustfs",
                "curl",
            ]
            assert argv[argv.index("-m") + 1] == "10"
            assert timeout == 15
            assert timeout > int(argv[argv.index("-m") + 1])
        if "--entrypoint" in argv:
            assert argv[argv.index("--network") + 1] == "none"
            assert argv[argv.index("--log-driver") + 1] == "none"
            assert argv[argv.index("--entrypoint") + 1] not in ("sh", "/bin/sh")
    assert engine.calls[-1][0][-10:] == [
        "rustfs:pinned",
        "-rf",
        "/d/alloy",
        "/d/config",
        "/d/grafana",
        "/d/loki",
        "/d/mimir",
        "/d/rustfs",
        "/d/secrets",
        "/d/tempo",
    ]


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("port", smoke.PORTS)
def test_bound_port_before_up(port: int) -> None:
    fs, engine, system = make_system()
    code, output = run(replace(system, ports=Ports(port)))
    assert code == 2
    assert "usage: loopback port already bound" in output
    assert not any("up" in argv for argv, _, _ in engine.calls)
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("path", [OUT, ARTIFACTS, HOST, OUT.parent])
def test_symlink_paths_refused(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, system = make_system()
    fs.modes[path] = stat.S_IFLNK
    monkeypatch.setattr(fs, "temporary_directory", lambda: HOST)
    code, output = run(system)
    assert code == 2
    assert "symlink path refused" in output
    assert not any(
        "up" in argv or "cp" in argv or "rm" in argv for argv, _, _ in engine.calls
    )


@pytest.mark.usefixtures("early_stages")
def test_interrupt_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, system = make_system()
    engine.fail_match = "up -d"
    original = engine.run

    def interrupted(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = original(argv, env, timeout, input_data=input_data)
        if "up" in argv:
            interrupt()
        return result

    monkeypatch.setattr(engine, "run", interrupted)
    code, output = run(system)
    assert code == 130
    assert "FAIL interrupted" in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    interrupted_up = next(
        i for i, (argv, _, _) in enumerate(engine.calls) if "up" in argv
    )
    assert any("down" in argv for argv, _, _ in engine.calls[interrupted_up + 1 :])


@pytest.mark.usefixtures("early_stages")
def test_early_exit_no_prerequisites() -> None:
    fs, engine, system = make_system()
    engine.fail_match = "config -q"
    code, output = run(system)
    assert code == 1
    assert "capture unavailable (stack not started)" in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
    assert not any("down" in argv for argv, _, _ in engine.calls)


def test_signal_handler_and_restore() -> None:
    previous = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    with pytest.raises(safety.SmokeError) as failure, command.signals():
        interrupt(signal.SIGTERM)
    assert failure.value.status == 143
    assert str(failure.value) == "interrupted"
    assert {n: signal.getsignal(n) for n in previous} == previous


def test_no_leak_flag() -> None:
    for flag in ("--leak",):
        with pytest.raises(SystemExit) as failure:
            cli.parser().parse_args(
                [
                    "o11y",
                    "smoke",
                    "--source",
                    "/src",
                    "--out",
                    "/out",
                    "--artifacts",
                    "/artifacts",
                    flag,
                    "5",
                ]
            )
        assert failure.value.code == 2


@pytest.mark.usefixtures("early_stages")
def test_generic_error_before_and_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, _, system = make_system()

    def fail() -> Path:
        raise OSError

    monkeypatch.setattr(fs, "temporary_directory", fail)
    assert run(system) == (
        1,
        "FAIL smoke: operation unavailable (OSError; before group 1)\n",
    )
    fs, engine, system = make_system()
    engine.fail_match = "config --format json"
    engine.replacement = Result(0, "invalid json")
    code, output = run(system)
    assert code == 1
    assert (
        "FAIL smoke: operation unavailable (JSONDecodeError; before group 1, step 4)\n"
        in output
    )
    assert fs.lstat(OUT) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("error", "label"),
    [
        (ValueError("private-error-sentinel"), "ValueError"),
        (
            ValueError("init: can't inspect 'grafana/grafana.db'; plan incomplete"),
            "ValueError",
        ),
        (KeyError("private-error-sentinel"), "KeyError"),
        (TypeError("private-error-sentinel"), "TypeError"),
        (CliError("private-error-sentinel"), "CliError"),
        (
            initialization.PlanIncompleteError("private-error-sentinel"),
            "PlanIncompleteError",
        ),
        *[
            (initialization.PlanIncompleteError(message), "PlanIncompleteError")
            for message in (
                "init: can't inspect grafana/grafana.db; plan incomplete",
                "init: can't inspect 'grafana/grafana.db'; plan incomplete\n",
                (
                    "private-error-sentinel init: can't inspect "
                    "'grafana/grafana.db'; plan incomplete"
                ),
                (
                    "init: can't inspect 'grafana/grafana.db'; plan incomplete "
                    "private-error-sentinel"
                ),
                (
                    "init: can't inspect 'grafana/\nprivate-error-sentinel'; "
                    "plan incomplete"
                ),
            )
        ],
        (OSError("private-error-sentinel"), "OSError"),
        (
            OSError(errno.ENOENT, "private-error-sentinel", "/private-file-sentinel"),
            "FileNotFoundError [ENOENT]",
        ),
        (OSError(-1, "private-error-sentinel", "/private-file-sentinel"), "OSError"),
    ],
)
def test_operation_failure_is_safe_and_identifies_group_step(
    error: Exception, label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine, system = make_system()
    original = engine.run

    def fail(
        argv: list[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        # The copy is the first command in group 2; permissions are the second.
        if "a+rX" in argv:
            raise error
        return original(argv, env, timeout, input_data=input_data)

    monkeypatch.setattr(engine, "run", fail)
    code, output = run(system)
    assert code == 1
    assert f"FAIL smoke: operation unavailable ({label}; group 2, step 2)\n" in output
    assert "private-error-sentinel" not in output
    assert "private-file-sentinel" not in output
    assert "ok 1:" in output
    assert "ok 2:" not in output


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    "relative",
    ["grafana/grafana.db", "grafana/operator's.db", "grafana/both'\"\\\n.db"],
)
def test_inspect_failure_preserves_only_data_relative_message(
    relative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, system = make_system()

    def denied(path: Path) -> NoReturn:
        raise PermissionError(
            errno.EACCES, "private-error-sentinel", str(path) + "private-file-sentinel"
        )

    def fail(instance: runtime.Smoke) -> None:
        instance.step = 20
        initialization._inspect(  # exercise the real safe exception boundary
            denied, instance.data / relative, instance.data, instance.system.euid
        )

    monkeypatch.setattr(bootstrap, "group2", fail)
    code, output = run(system)
    assert code == 1
    failures = [line for line in output.splitlines() if line.startswith("FAIL ")]
    assert failures == [
        (
            "FAIL smoke: operation unavailable "
            "(PlanIncompleteError; group 2, step 20): "
            f"init: can't inspect {relative!r}; plan incomplete"
        )
    ]
    assert "private-error-sentinel" not in output
    assert "private-file-sentinel" not in output
    assert "/var/lib/" not in output
    assert str(HOST) not in output


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("module", "name", "group"),
    [
        (setup_stage, "group1", "group 1"),
        (bootstrap, "group2", "group 2"),
        (readiness, "group3", "group 3"),
        (readiness, "group3a", "group 3a"),
        (grafana, "group4", "group 4"),
        (exports, "group5", "group 5"),
        (exports, "export_persistence", "export persistence"),
        (cap, "smoke", "capacity"),
        (exports, "group6", "group 6"),
        (monitoring, "group7", "group 7"),
        (lifecycle, "group8", "group 8"),
        (lifecycle, "group9", "group 9"),
        (lifecycle, "group10", "group 10"),
        (telemetry, "group11", "group 11"),
        (alerts, "group12", "group 12"),
        (lifecycle, "group13", "group 13"),
        (cap, "cleanup", "capacity cleanup"),
    ],
)
def test_each_stage_reports_its_own_group_and_resets_step(
    module: object, name: str, group: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, system = make_system()

    def fail(_run: runtime.Smoke, *_args: object) -> None:
        message = "private-error-sentinel"
        raise KeyError(message)

    monkeypatch.setattr(module, name, fail)
    code, output = run(system)
    assert code == 1
    assert f"FAIL smoke: operation unavailable (KeyError; {group}, step 1)\n" in output
    assert "private-error-sentinel" not in output


@pytest.mark.usefixtures("early_stages")
def test_cleanup_before_output_and_early_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, system = make_system()
    engine.interrupt_at = 1
    code, output = run(system)
    assert code == 130
    assert fs.lstat(HOST) is None
    assert "capture unavailable" in output
    fs, _, system = make_system()

    def interrupted(
        _args: argparse.Namespace, _system: cli.System, _out: io.StringIO
    ) -> int:
        interrupt()

    # Directly test that the dispatch context restores handlers on BaseException.
    monkeypatch.setattr(command, "_dispatch", interrupted)
    previous = signal.getsignal(signal.SIGINT)
    with pytest.raises(safety.SmokeError) as failure:
        run(system)
    assert failure.value.status == 130
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_early_signal_interrupt(number: int, monkeypatch: pytest.MonkeyPatch) -> None:
    fs, _, system = make_system()

    def fail() -> Path:
        interrupt(number)

    monkeypatch.setattr(fs, "temporary_directory", fail)
    assert run(system) == (128 + number, "FAIL interrupted\n")


@pytest.mark.parametrize(
    ("out", "artifacts", "source"),
    [
        ("/same", "/same", "/src"),
        ("/parent", "/parent/child", "/src"),
        ("/parent/child", "/parent", "/src"),
        ("/src", "/artifacts", "/src"),
        ("/src/child", "/artifacts", "/src"),
        ("/src", "/artifacts", "/src/child"),
        ("/out", "/src", "/src"),
    ],
)
def test_overlapping_paths_usage(out: str, artifacts: str, source: str) -> None:
    _, engine, system = make_system()
    output = io.StringIO()
    args = argparse.Namespace(
        out=Path(out), artifacts=Path(artifacts), source=Path(source)
    )
    assert command.dispatch(args, system, output) == 2
    assert output.getvalue() == "FAIL usage: overlapping paths\n"
    assert engine.calls == []


@pytest.mark.usefixtures("early_stages")
def test_local_engine_refusal() -> None:
    fs, engine, system = make_system()
    engine.identity = "other|Linux"
    code, output = run(system)
    assert code == 1
    assert (
        output == "engine mode: local\nFAIL setup: local engine refused\n"
        "smoke diagnostics: capture unavailable (stack not started)\n"
    )
    assert fs.lstat(HOST) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_interrupt_during_random_allocation_cleans_temp(
    cleanup_fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, _, system = make_system()

    def random(_size: int) -> bytes:
        interrupt()

    if cleanup_fails:

        def remove(_path: Path) -> None:
            raise OSError

        monkeypatch.setattr(fs, "rmdir", remove)
    code, output = run(replace(system, random_bytes=random))
    assert code == 130
    if cleanup_fails:
        assert output == "FAIL interrupted\nsmoke diagnostics: cleanup failed\n"
    else:
        assert output == "FAIL interrupted\n"
        assert fs.lstat(HOST) is None


def test_smoke_parser_contract(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli.parser()
    flags = ["--source", "/source", "--out", "/out", "--artifacts", "/artifacts"]
    args = parser.parse_args(["o11y", "smoke", *flags])
    assert vars(args) == {
        "group": "o11y",
        "command": "smoke",
        "dedicated_engine": None,
        "source": Path("/source"),
        "out": Path("/out"),
        "artifacts": Path("/artifacts"),
    }
    for index in range(0, len(flags), 2):
        with pytest.raises(SystemExit) as failure:
            parser.parse_args(["o11y", "smoke", *flags[:index], *flags[index + 2 :]])
        assert failure.value.code == 2
        assert flags[index] in capsys.readouterr().err
    with pytest.raises(SystemExit) as failure:
        parser.parse_args(["o11y", "--help"])
    assert failure.value.code == 0
    assert "smoke exercise a disposable local stack" in " ".join(
        capsys.readouterr().out.split()
    )


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("error", "expected", "status"),
    [
        (safety.SmokeError("classified failure"), "FAIL classified failure\n", 1),
        (signal.SIGINT, "FAIL interrupted\n", 130),
        (
            OSError(),
            "FAIL smoke: operation unavailable (OSError; group 2, step 1)\n",
            1,
        ),
    ],
)
def test_dispatch_failure_is_retained_in_transcript(
    error: BaseException | int,
    expected: str,
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, system = make_system()
    original = diagnostics.finish
    finished = []

    def group2(_instance: runtime.Smoke) -> None:
        if isinstance(error, int):
            interrupt(error)
        else:
            raise error

    def finish(instance: runtime.Smoke, code: int, output: io.StringIO) -> None:
        assert instance.transcript[-1] == expected
        assert code == status
        finished.append(code)
        original(instance, code, output)

    monkeypatch.setattr(bootstrap, "group2", group2)
    monkeypatch.setattr(diagnostics, "finish", finish)
    code, output = run(system)
    assert code == status
    assert finished == [status]
    assert output.startswith("engine mode: local\nsmoke setup\nok 1:")
    assert fs.lstat(HOST) is None
    assert fs.lstat(OUT) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("kind", ["absent", "empty", "file", "nonempty"])
def test_artifacts_private_directory(kind: str) -> None:
    fs, engine, system = make_system()
    if kind == "file":
        fs.write_atomic(ARTIFACTS, "existing", 0o644)
    elif kind != "absent":
        fs.mkdir(ARTIFACTS)
        if kind == "nonempty":
            fs.write_atomic(ARTIFACTS / "existing", "keep", 0o644)
    code, output = run(system)
    if kind in ("file", "nonempty"):
        assert code == 2
        assert "output must be an empty directory" in output
        existing = ARTIFACTS if kind == "file" else ARTIFACTS / "existing"
        assert fs.read_text(existing) == ("existing" if kind == "file" else "keep")
        assert not any("up" in argv for argv, _, _ in engine.calls)
    else:
        assert code == 0, output
        assert fs.list_dir(ARTIFACTS) == ()
        assert fs.modes[ARTIFACTS] & 0o777 == 0o700


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("dedicated", [None, "unix:///run/dind/docker.sock"])
@pytest.mark.parametrize("leftovers", [False, True])
def test_dedicated_smoke_dispatch(
    dedicated: str | None,
    leftovers: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, engine, system = make_system()
    endpoint = "unix:///run/dind/docker.sock"
    fs.modes[Path("/run/dind/docker.sock")] = stat.S_IFSOCK | 0o600
    original = engine.run

    def metadata(
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        assert input_data is None
        responses: dict[tuple[str, ...], str] = {
            (
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                "default",
            ): endpoint,
            ("ps", "-aq"): "stopped-container\n" if leftovers else "",
            ("volume", "ls", "-q"): "",
            ("network", "ls", "--format", "{{.Name}}"): "bridge\nhost\nnone\n",
        }
        if tuple(argv[1:]) in responses:
            engine.calls.append((list(argv), dict(env), timeout))
            return Result(0, responses[tuple(argv[1:])])
        return original(argv, env, timeout)

    monkeypatch.setattr(engine, "run", metadata)
    system = replace(
        system,
        env=dict(system.env)
        | {"O11Y_DEDICATED_ENGINE": endpoint, "DEDICATED_ENGINE": endpoint},
    )
    args = [
        "o11y",
        "smoke",
        "--source",
        str(SOURCE),
        "--out",
        str(OUT),
        "--artifacts",
        str(ARTIFACTS),
    ]
    if dedicated is not None:
        args.extend(["--dedicated-engine", dedicated])
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(args, system, out, err)
    output = out.getvalue()
    mode = "local" if dedicated is None else f"dedicated {dedicated}"
    assert output.startswith(f"engine mode: {mode}\n")
    assert output.count("engine mode:") == 1
    assert err.getvalue() == ""
    if dedicated is None or leftovers:
        assert code == 1
        assert "FAIL setup: local engine refused" in output
        assert not any(argv[1] == "compose" for argv, _, _ in engine.calls)
    else:
        assert code == 0, output
        assert "ok 3a:" in output
    if dedicated is None:
        assert all("DOCKER_HOST" not in env for _, env, _ in engine.calls)
    else:
        assert all(env["DOCKER_HOST"] == endpoint for _, env, _ in engine.calls)


@pytest.mark.usefixtures("before_monitoring")
def test_full_flow_and_mimir_failure_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    for fail in (False, True):
        fs, _engine, http, system = setup_b()
        original_group4 = grafana.group4

        def group4(
            run: runtime.Smoke,
            http: Http = http,
            original_group4: Callable[[runtime.Smoke], None] = original_group4,
        ) -> None:
            http.webhook = dict(run.sentinels.values)[init.WEBHOOK].decode()
            original_group4(run)

        with monkeypatch.context() as patch:
            patch.setattr(grafana, "group4", group4)
            if fail:
                http.overrides["/uid/mimir/api/v1/query_range?"] = (
                    200,
                    b"private-body",
                )
            code, output, error = execute(system)
        assert code == int(fail), output + error
        assert error == ""
        assert "private-body" not in output
        assert ("FAIL export persistence: sustained-loss" in output) == fail, output
        assert ("ok 6:" in output) != fail
        assert fs.lstat(OUT) is fs.lstat(HOST) is None


@pytest.mark.usefixtures("before_monitoring")
@pytest.mark.parametrize("group", [5, 6])
def test_receipt_failure_transcript_order(
    group: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, http, system = setup_b()
    original_group4 = grafana.group4

    def group4(run: runtime.Smoke) -> None:
        http.webhook = dict(run.sentinels.values)[init.WEBHOOK].decode()
        original_group4(run)

    monkeypatch.setattr(grafana, "group4", group4)
    if group == 5:
        http.overrides["/v1/logs"] = (500, b"private-body")
        rule = "5: receipt"
    else:
        original_group6 = exports.group6

        def group6(run: runtime.Smoke) -> None:
            http.overrides["/loki/"] = (500, b"private-body")
            original_group6(run)

        monkeypatch.setattr(exports, "group6", group6)
        rule = "6: receipt-recheck"
    code, output, error = execute(system)
    assert code == 1
    assert error == ""
    reason = "signal-check"
    assert f"FAIL receipt: {reason}\nFAIL {rule}\n" in output
    assert "private-body" not in output


@pytest.mark.usefixtures("before_monitoring")
@pytest.mark.parametrize("dedicated", [None, "unix:///run/dind/docker.sock"])
def test_group5_dispatch_engine_environment(
    dedicated: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, engine, http, system = setup_b()
    original_group4 = grafana.group4
    original_finalize = runtime.Smoke.finalize_transcript
    finalized = []

    def finalize(run: runtime.Smoke) -> None:
        original_finalize(run)
        finalized.append(fs.read_text(OUT / "transcript.log"))

    def group4(run: runtime.Smoke) -> None:
        http.webhook = dict(run.sentinels.values)[init.WEBHOOK].decode()
        original_group4(run)

    monkeypatch.setattr(grafana, "group4", group4)
    monkeypatch.setattr(runtime.Smoke, "finalize_transcript", finalize)
    if dedicated is not None:
        fs.modes[Path("/run/dind/docker.sock")] = stat.S_IFSOCK | 0o600
        engine.overrides.update(
            {
                "context inspect": Result(0, dedicated),
                "ps -aq": Result(0),
                "volume ls -q": Result(0),
                "network ls --format": Result(0, "bridge\nhost\nnone\n"),
            }
        )
    args = ["--dedicated-engine", dedicated] if dedicated is not None else []
    code, output, error = execute(system, *args)
    assert code == 0, output
    assert error == ""
    assert finalized == [output, output]
    assert fs.lstat(OUT) is fs.lstat(HOST) is None
    assert any("down" in argv for argv, _, _ in engine.calls)
    calls = [call for call in engine.calls if "telemetrygen:pinned" in call[0]]
    assert len(calls) == 1
    argv, env, _ = calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "ok 5: receipt" in output
    assert "AWS_SECRET_ACCESS_KEY" not in env
    if dedicated is None:
        assert "DOCKER_HOST" not in env
    else:
        assert env["DOCKER_HOST"] == dedicated


def test_flow_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, run = instance_c()
    events: list[str] = json.loads("[]")

    def recorder(name: str) -> Callable[..., None]:
        def record(*_args: object) -> None:
            if name == "finalize_transcript":
                assert _args == ()
            elif name in {"prepare", "smoke", "group7", "group8", "group12", "group13"}:
                assert _args == (run, STACK_SOURCE)
            else:
                assert _args == (run,)
            events.append(name)

        return record

    monkeypatch.setattr(run, "finalize_transcript", recorder("finalize_transcript"))
    for module, names in (
        (setup_stage, ("prepare", "group1")),
        (bootstrap, ("group2",)),
        (readiness, ("group3", "group3a")),
        (diagnostics, ("scans",)),
        (grafana, ("group4",)),
        (exports, ("group5", "export_persistence", "group6")),
        (monitoring, ("group7",)),
        (telemetry, ("group11",)),
        (alerts, ("group12",)),
        (lifecycle, ("group8", "group9", "group10", "group13")),
        (cap, ("smoke", "cleanup")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, recorder(name))
    execute_stages: Callable[[runtime.Smoke, Path], None] = vars(command)["_execute"]
    execute_stages(run, STACK_SOURCE)
    assert events == [
        "prepare",
        "group1",
        "group2",
        "group3",
        "group3a",
        "group4",
        "group5",
        "export_persistence",
        "smoke",
        "group6",
        "group7",
        "group8",
        "group9",
        "group10",
        "group11",
        "group12",
        "group13",
        "cleanup",
        "finalize_transcript",
        "scans",
    ]
