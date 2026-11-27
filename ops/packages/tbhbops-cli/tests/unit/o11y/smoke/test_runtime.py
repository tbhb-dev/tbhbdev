"""Smoke process execution and run state."""

import json
import stat
from collections import Counter
from dataclasses import replace
from pathlib import Path
lazy from collections.abc import Callable

import pytest
from tbhbops.o11y import init, smoke
from tbhbops_cli.o11y import render
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    runtime,
    safety,
)
from tbhbops_cli.o11y.smoke import setup as setup_stage
from test_o11y_cli import SOURCE
from unit.o11y.smoke.runtime_fixture import HOST, OUT, make_system, prepared, run

TESTDATA = Path(__file__).parents[3] / "testdata"


def test_environment_builder_wal_override() -> None:
    env = runtime.environment(
        {
            "PATH": "p",
            "HOME": "h",
            "TMPDIR": "t",
            "DOCKER_CONFIG": "c",
            "DOCKER_CONTEXT": "remote",
            "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD": "0.1",
            "COMPOSE_PROFILES": "bad",
        },
        HOST,
        (1, 2),
    )
    assert env == {
        **smoke.FIXED_ENV,
        "PATH": "p",
        "HOME": "h",
        "TMPDIR": "t",
        "DOCKER_CONFIG": "c",
        "O11Y_DATA_DIR": str(HOST),
        "O11Y_SECRETS_OWNER": "1:2",
    }
    assert (
        runtime.environment({}, HOST, (1, 2))["O11Y_LOKI_WAL_DISK_FULL_THRESHOLD"]
        == "0.99"
    )


@pytest.mark.usefixtures("early_stages")
def test_macos_host_env_guard_and_cleanup() -> None:
    fs, engine, system = make_system()
    engine.identity = "docker-desktop|Docker Desktop"
    code, output = run(replace(system, platform="darwin"))
    assert code == 0, output
    commands = [argv for argv, _, _ in engine.calls]
    copies = [argv for argv in commands if "cp" in argv]
    assert any(argv[-2:] == ["/host/.", "/d/smoke-01010101/"] for argv in copies)
    assert any(
        argv[-2:] == ["/d/smoke-01010101/rustfs/.rustfs.sys", "/host/rustfs/"]
        for argv in copies
    )
    assert any(
        argv[-2:] == ["/host/secrets/.", "/d/smoke-01010101/secrets/"]
        for argv in copies
    )
    for argv, env, _ in engine.calls:
        assert env["O11Y_DATA_DIR"] == "/var/lib/o11y-test/smoke-01010101"
        if "up" in argv:
            assert env["O11Y_S3_ENV_FILE"] == "/temporary/secrets/s3.env"
    assert fs.lstat(HOST) is None
    assert fs.lstat(Path("/var/lib/o11y-test/smoke-01010101")) is None
    assert fs.lstat(OUT) is None


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    "path", [Path("/var/lib/o11y-test/smoke-01010101"), Path("/var/lib/o11y-test")]
)
def test_macos_symlink_data_refused(path: Path) -> None:
    fs, engine, system = make_system()
    engine.identity = "docker-desktop|Docker Desktop"
    fs.modes[path] = stat.S_IFLNK
    code, output = run(replace(system, platform="darwin"))
    assert code == 1
    assert "data dir symlink or inspection failure" in output
    assert not any("cp" in argv for argv, _, _ in engine.calls)
    assert fs.modes[path] == stat.S_IFLNK
    assert fs.lstat(OUT) is None


def test_output_cap_and_inside_status() -> None:
    _, engine, instance = prepared()
    for code in (0, 17):
        engine.fail_match = "exec"
        engine.replacement = Result(code, "body\n200", "private")
        assert (
            instance.inside("http://loki:3100/config", body=True) == engine.replacement
        )
        argv, _, timeout = engine.calls[-1]
        assert argv[argv.index("curl") :] == [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            "-m",
            "10",
            "http://loki:3100/config",
        ]
        assert timeout == 15
    engine.replacement = Result(0, "A" * smoke.LIMIT)
    assert instance.inside("http://loki:3100/config", body=True) == engine.replacement
    engine.replacement = Result(0, "A" * smoke.LIMIT, "b")
    with pytest.raises(safety.SmokeError, match="output exceeds 4 MiB"):
        instance.inside("http://loki:3100/config", body=True)


def test_container_ids_and_missing_ps() -> None:
    _, engine, instance = prepared()
    states = instance.container_ids()
    assert states == {name: (name, "2026-10-25T05:38:26Z") for name in smoke.SERVICES}
    for replacement in (Result(17, "private"), Result(0, "")):
        engine.fail_match = "ps -q"
        engine.replacement = replacement
        with pytest.raises(safety.SmokeError, match="container state unavailable"):
            instance.container_ids()


def test_utility_refuses_outside_root_and_empty_reclaim() -> None:
    fs, _, instance = prepared()
    other = replace(instance, data=Path("/unsafe"))
    with pytest.raises(safety.SmokeError, match="unsafe data dir"):
        other.utility(["rm", "-rf", "/unsafe"])
    other = replace(instance, system=replace(instance.system, platform="darwin"))
    with pytest.raises(safety.SmokeError, match="unsafe data dir"):
        other.utility(["rm", "-rf", "/unsafe"])
    render.remove_tree(fs, HOST / "secrets")
    setup_stage.reclaim(instance)


def test_stage_forwards_keyword_arguments() -> None:
    _, _, instance = prepared()

    def operation(*, source: Path) -> None:
        assert source == SOURCE

    instance.stage("group 2", operation, source=SOURCE)


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_compose_and_utility_command_contract(platform: str) -> None:
    fs, engine, system = make_system()
    if platform == "darwin":
        engine.identity = "docker-desktop|Docker Desktop"
        system = replace(system, platform=platform)
    assert run(system)[0] == 0
    counts = Counter((tuple(argv), timeout) for argv, _, timeout in engine.calls)
    actual = [
        {"argv": list(argv), "timeout": timeout, "count": count}
        for (argv, timeout), count in counts.items()
    ]
    fixture = TESTDATA / "smoke-commands.json"
    expected = [
        record
        for record in json.loads(fixture.read_text())
        if record.pop("platform", platform) == platform
    ]
    assert actual == expected
    for call in fs.calls:
        if (
            call[0] == "write"
            and OUT in call[1].parents
            and call[1].name.endswith((".log", "fixture.yaml"))
        ):
            # render writes public-readable configs, then smoke makes them private;
            # everything created by smoke itself starts private.
            assert call[2] == 0o600


def test_disk_observation_phases() -> None:
    _, _, instance = prepared()
    instance.record_loki_disk("started")
    instance.record_loki_disk("before-group-5")
    assert instance.transcript[-2:] == [
        "smoke Loki disk usage (started): 0.800000\n",
        "smoke Loki disk usage (before-group-5): 0.800000\n",
    ]


def test_utility_link_inspection_and_owner_failures() -> None:
    _, engine, instance = prepared()
    instance = replace(
        instance,
        system=replace(instance.system, platform="darwin"),
        data=Path("/var/lib/o11y-test/smoke-01010101"),
    )
    engine.fail_match = "--entrypoint test rustfs:pinned -L /d/"
    engine.replacement = Result(0)
    with pytest.raises(safety.SmokeError) as failure:
        instance.utility(["rm", "-rf", instance.destination])
    assert str(failure.value) == "data dir symlink or inspection failure"
    engine.fail_match = "no-match"
    assert isinstance(setup_stage.initialize(instance), init.Plan)
    engine.fail_match = "--entrypoint chown"
    engine.replacement = Result(17)
    with pytest.raises(safety.SmokeError) as failure:
        setup_stage.sync_data(instance)
    assert str(failure.value) == "init: owner update failed"


@pytest.mark.usefixtures("early_stages")
def test_macos_data_names_follow_run() -> None:
    for token in (b"\x12\xab\x34\xcd", b"\x56\xef\x78\x90"):
        fs, engine, system = make_system()
        engine.identity = "docker-desktop|Docker Desktop"
        original = system.random_bytes

        def random(
            size: int, original: Callable[[int], bytes] = original, token: bytes = token
        ) -> bytes:
            value = original(size)
            return token if size == 4 else value

        code, output = run(replace(system, platform="darwin", random_bytes=random))
        assert code == 0, output
        expected = "/var/lib/o11y-test/smoke-" + token.hex()
        assert {env["O11Y_DATA_DIR"] for _, env, _ in engine.calls} == {expected}
        for argv, _, _ in engine.calls:
            if (
                "--entrypoint" in argv
                and argv[argv.index("--entrypoint") + 1] == "test"
            ):
                assert argv[-1] in (
                    "/parent/o11y-test",
                    "/parent/o11y-test/smoke-" + token.hex(),
                    "/d/smoke-" + token.hex(),
                )
        assert fs.lstat(Path(expected)) is None


def test_linux_mount_is_data_tree() -> None:
    _, engine, instance = prepared()
    assert instance.utility(["chmod", "700", "/d"]).code == 0
    for argv, _, _ in engine.calls:
        if argv[1] == "run":
            assert argv[argv.index("--entrypoint") + 1] != "test"
            assert argv[argv.index("-v") + 1] == "/temporary:/d"
            assert "/:/d" not in argv


def test_compose_head_budget_and_bounds() -> None:
    _, engine, instance = prepared()
    args = ["logs", "--no-color", "loki"]
    result = instance.compose(args, head=True, timeout=37)
    assert result == Result(0, "".join(f"{n}\n" for n in range(1, 201)))
    assert len(engine.head_calls) == 1
    argv, timeout, byte_limit, line_limit = engine.head_calls[0]
    assert argv[-3:] == args
    assert (timeout, byte_limit, line_limit) == (37, 4 * 1024 * 1024, 200)
