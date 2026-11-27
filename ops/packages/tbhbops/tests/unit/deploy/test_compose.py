"""Test isolated Compose parsing and stored release operations."""

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest
from tbhbops.deploy import compose, daemon, policy
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy.command import OUTPUT_LIMIT, Command, CommandError
from tbhbops.deploy.compose import COMPOSE_SHA256, Engine
from tbhbops.deploy.protocol import Json, RefusalError, array, mapping
from tbhbops.deploy.transaction import Transaction

from ...deploy_fakes import RELEASE, FakeCommand
from ...deploy_model import minimal


def test_parse_refuses_a_changed_fixed_point(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fake.fixed = {"changed": True}
    with pytest.raises(RefusalError, match=r"^Compose fixed point$"):
        engine.parse(RELEASE)


def test_preflight_validates_unpublished_files_before_daemon_read(
    engine: Engine,
) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    original = engine.sandbox
    seen: list[Path] = []

    def inspect_staging(
        release: str,
        source: Path,
        env: Path,
        *,
        fixed: bool,
        staging: Path | None = None,
    ) -> Json:
        assert release == RELEASE
        assert fixed is False
        assert staging is not None
        seen.append(staging)
        assert source == staging / engine.service.compose_file
        assert env == staging / engine.service.compose_env_file
        assert source.read_bytes() == b"compose input"
        assert env.read_bytes() == b"FOO=value\n"
        assert (
            staging / "nested" / "deeper" / "config"
        ).read_bytes() == b"config input"
        assert fake.calls == []
        return original(release, source, env, fixed=fixed, staging=staging)

    files = {
        "release/" + engine.service.compose_file: b"compose input",
        "release/" + engine.service.compose_env_file: b"FOO=value\n",
        "release/nested/deeper/config": b"config input",
    }
    with patch.object(engine, "sandbox", side_effect=inspect_staging):
        engine.preflight(RELEASE, files)
    staging = seen[0]
    assert not staging.exists()
    assert not engine.model_path(RELEASE).exists()
    parse, info = fake.calls
    assert parse[1] is None
    assert "--property=BindReadOnlyPaths=/compose " + str(staging) in parse[0]
    unit = "tbhb-deploy-parse-o11y-" + staging.name
    assert "--unit=" + unit in parse[0]
    assert fake.budgets[0]["unit"] == unit
    assert (
        str(engine.service.root / "releases" / RELEASE)
        == parse[0][parse[0].index("--project-directory") + 1]
    )
    assert info == (["/usr/bin/docker", "info", "--format", "{{json .}}"], None)


def test_preflight_model_refusal_does_not_read_daemon(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fake.model = minimal(pids_limit=0)
    with pytest.raises(RefusalError):
        engine.preflight(RELEASE, {"release/compose.yaml": b"invalid model"})
    assert len(fake.calls) == 1
    assert "config" in fake.calls[0][0]
    assert not engine.model_path(RELEASE).exists()


def test_stop_without_project_containers(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    engine.stop()
    assert [argv[1:3] for argv, _ in fake.calls] == [["ps", "-aq"]]


def test_stored_and_container_writable_set(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    data = engine.service.root / "data"
    data.mkdir()
    (data / "web").mkdir()
    (data / "web" / "config").write_bytes(b"x")
    fake.inspected = [
        {
            "Id": "abc",
            "Mounts": [
                {"Type": "volume", "RW": True},
                {"Type": "bind", "RW": False, "Source": str(data / "other")},
                {"Type": "bind", "RW": True, "Source": str(data / "web")},
            ],
        }
    ]
    candidate: Json = minimal(
        volumes=[
            {
                "type": "bind",
                "source": str(data / "web" / "config"),
                "target": "/etc/config",
                "read_only": True,
            }
        ]
    )
    fs.write(engine.model_path("b" * 40), json.dumps(fake.model).encode())
    with pytest.raises(RefusalError, match="writable bind"):
        engine.check_paths(candidate, RELEASE)
    fake.inspected = [{"Id": "abc", "Mounts": "invalid"}]
    with pytest.raises(RefusalError, match=r"^container mounts type$"):
        engine.check_paths(candidate, RELEASE)
    fake.inspected = []
    engine.check_paths(candidate, RELEASE)
    candidate = minimal(env_file=[{"path": str(engine.service.secrets[0].target)}])
    engine.check_paths(candidate, RELEASE, missing=True)
    with pytest.raises(RefusalError, match="missing"):
        engine.check_paths(candidate, RELEASE)
    candidate = minimal(
        volumes=[
            {
                "type": "bind",
                "source": str(data / "secrets" / "generated"),
                "target": "/secret",
                "read_only": True,
            }
        ]
    )
    engine.check_paths(candidate, RELEASE, missing=True)


def test_up_rechecks_and_force_recreates(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fs.write(engine.model_path(RELEASE), json.dumps(fake.model).encode())
    checked: list[str] = []
    monkeypatch.setattr(engine, "health", checked.append)
    engine.up(RELEASE, recreate=True)
    assert "--force-recreate" in fake.calls[-1][0]
    engine.up(RELEASE, recreate=False)
    assert "--force-recreate" not in fake.calls[-1][0]
    assert checked == [RELEASE, RELEASE]


def healthy() -> Json:
    return [
        {
            "Id": "abc",
            "Config": {"Labels": {"com.docker.compose.service": "web"}},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
    ]


def test_health_states(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fake.inspected = healthy()
    engine.check_health(RELEASE)
    assert fake.calls[-1][0][-4:] == ["exec", "-T", "web", "true"]
    fake.inspected = [
        {
            "Config": {"Labels": {"com.docker.compose.service": "web"}},
            "State": {"Running": True},
        }
    ]
    engine.check_health(RELEASE)
    engine.health(RELEASE)
    fake.inspected = []
    now = 0.0
    monkeypatch.setattr("tbhbops.deploy.compose.time.monotonic", lambda: now)
    calls = 0

    def eventually_healthy(seconds: float) -> None:
        nonlocal calls, now
        calls += 1
        now += seconds
        fake.inspected = healthy()

    monkeypatch.setattr("tbhbops.deploy.compose.time.sleep", eventually_healthy)
    engine.health(RELEASE)
    assert calls == 1
    assert now == 1
    fake.inspected = [
        {
            "Config": {"Labels": {"com.docker.compose.service": "web"}},
            "State": {"Running": False},
        }
    ]
    with pytest.raises(CommandError, match=r"^container is not running$"):
        engine.check_health(RELEASE)
    fake.inspected = [
        {
            "Config": {"Labels": {"com.docker.compose.service": "web"}},
            "State": {"Running": True, "Health": {"Status": "unhealthy"}},
        }
    ]
    with pytest.raises(CommandError, match=r"^container is not healthy$"):
        engine.check_health(RELEASE)


def test_exact_run_command(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    engine.run("b" * 40, "exec", "-T", "db", "probe", "--ready")
    assert fake.calls == [
        (
            [
                "/usr/bin/systemd-run",
                "--unit=tbhb-deploy-o11y",
                "--collect",
                "--wait",
                "--pipe",
                "/compose",
                "-p",
                "o11y",
                "--project-directory",
                str(engine.service.root / "releases" / ("b" * 40)),
                "-f",
                str(engine.state / "models" / ("b" * 40 + ".json")),
                "--env-file",
                "/dev/null",
                "exec",
                "-T",
                "db",
                "probe",
                "--ready",
            ],
            None,
        )
    ]


@pytest.mark.parametrize("recreate", [False, True])
def test_up_call_order_and_arguments(engine: Engine, recreate: bool) -> None:
    candidate: Json = {"name": "o11y"}
    with (
        patch.object(engine, "stored", return_value=candidate) as stored,
        patch.object(engine, "check_paths") as paths,
        patch.object(engine, "run") as run,
        patch.object(engine, "health") as health,
    ):
        manager = Mock()
        manager.attach_mock(stored, "stored")
        manager.attach_mock(paths, "paths")
        manager.attach_mock(run, "run")
        manager.attach_mock(health, "health")
        engine.up(RELEASE, recreate=recreate)
    flags = ("--force-recreate",) if recreate else ()
    assert manager.mock_calls == [
        call.stored(RELEASE),
        call.paths(candidate, RELEASE, live=False),
        call.run(RELEASE, "up", "-d", "--wait", "--remove-orphans", *flags),
        call.health(RELEASE),
    ]


@pytest.mark.parametrize(
    ("missing", "source", "expected"),
    [
        (False, policy.Source(Path("/data/file"), file=True), False),
        (True, policy.Source(Path("/data/file"), file=True), False),
        (True, policy.Source(Path("/data"), writable=True), False),
    ],
)
def test_inspect_sources_keeps_external_missing_strict(
    engine: Engine, missing: bool, source: policy.Source, expected: bool
) -> None:
    with patch.object(fs, "inspect") as inspect:
        engine.inspect_sources((source,), missing=missing)
    inspect.assert_called_once_with(source.path, file=source.file, missing=expected)


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize(
    ("writable", "file"), [(False, False), (True, False), (True, True)]
)
def test_generated_source_missing_rules(
    engine: Engine, missing: bool, writable: bool, file: bool
) -> None:
    path = engine.service.root / "data" / "secrets" / "generated"
    with patch.object(fs, "inspect") as inspect:
        engine.inspect_sources(
            (policy.Source(path, writable=writable, file=file),), missing=missing
        )
    inspect.assert_called_once_with(
        path, file=file, missing=missing and (file or not writable)
    )


@pytest.mark.parametrize("elapsed", [5, 9, 9.999, 10, 10.001])
def test_health_deadline_and_retry_interval(engine: Engine, elapsed: float) -> None:
    with (
        patch(
            "tbhbops.deploy.compose.time.monotonic",
            side_effect=[100, 100 + elapsed, 100 + elapsed],
        ),
        patch("tbhbops.deploy.compose.time.sleep") as sleep,
        patch.object(
            engine, "check_health", side_effect=[CommandError("pending"), None]
        ) as check,
    ):
        if elapsed >= 10:
            message = "expired health gate retried"
            sleep.side_effect = AssertionError(message)
            with pytest.raises(CommandError, match=r"^health gate failed$"):
                engine.health(RELEASE)
            sleep.assert_not_called()
            check.assert_called_once_with(RELEASE, deadline=110)
        else:
            engine.health(RELEASE)
            sleep.assert_called_once_with(pytest.approx(min(1, 10 - elapsed)))
            assert check.call_args_list == [
                call(RELEASE, deadline=110),
                call(RELEASE, deadline=110),
            ]


def test_parse_checks_and_persists_the_fixed_model_in_order(engine: Engine) -> None:
    candidate: Json = {"z": 1, "a": 2}
    with (
        patch.object(engine, "sandbox", return_value=candidate) as sandbox,
        patch.object(policy, "model") as model,
        patch.object(engine, "check_paths") as paths,
        patch.object(fs, "write") as write,
        patch.object(daemon, "ownership") as ownership,
    ):
        manager = Mock()
        for name, method in (
            ("sandbox", sandbox),
            ("model", model),
            ("paths", paths),
            ("write", write),
            ("ownership", ownership),
        ):
            manager.attach_mock(method, name)
        engine.parse(RELEASE)
    directory = engine.service.root / "releases" / RELEASE
    assert manager.mock_calls == [
        call.sandbox(
            RELEASE,
            directory / "compose.yaml",
            directory / "compose.env",
            fixed=False,
        ),
        call.model(candidate, engine.service),
        call.paths(candidate, RELEASE, missing=True),
        call.write(engine.model_path(RELEASE), b'{"a": 2, "z": 1}'),
        call.sandbox(
            RELEASE, engine.model_path(RELEASE), Path("/dev/null"), fixed=True
        ),
        call.ownership(engine.command, engine.service.name, candidate),
    ]


def test_container_queries_preserve_all_identifiers(engine: Engine) -> None:
    with patch.object(
        engine, "command", side_effect=[b"abc123\nDEF456\n", b'[{"Id":"abc123"}]']
    ) as command:
        assert engine.containers() == [{"Id": "abc123"}]
    assert command.call_args_list == [
        call(
            [
                "/usr/bin/docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=o11y",
            ]
        ),
        call(["/usr/bin/docker", "inspect", "abc123", "DEF456"]),
    ]


@pytest.mark.parametrize(
    ("outputs", "reason"),
    [
        ([b"invalid/id"], "container identifiers"),
        ([b"abc", b"{}"], "container inspect type"),
    ],
)
def test_container_query_refusal_reasons(
    engine: Engine, outputs: list[bytes], reason: str
) -> None:
    with (
        patch.object(engine, "command", side_effect=outputs),
        pytest.raises(RefusalError) as caught,
    ):
        engine.containers()
    assert str(caught.value) == reason


def test_unit_liveness_checks_both_units(engine: Engine) -> None:
    with patch.object(Command, "stop_unit") as stop:
        engine.stop_unit()
    assert stop.call_args_list == [
        call("tbhb-deploy-o11y"),
        call("tbhb-deploy-parse-o11y"),
    ]


def test_retained_models_and_live_binds_reach_path_policy(engine: Engine) -> None:
    directory = engine.state / "models"
    for name in ("b.json", "a.json", "ignored.txt"):
        (directory / name).write_text(name)
    candidate: Json = {"candidate": True}
    sources = (policy.Source(Path("/candidate"), writable=True),)
    retained = (
        policy.Source(Path("/retained/a")),
        policy.Source(Path("/retained/b"), writable=True),
    )
    containers: list[dict[str, Json]] = [
        {
            "Mounts": [
                {"Type": "bind", "RW": True, "Source": "/live"},
                {"Type": "bind", "RW": False, "Source": "/read-only"},
                {"Type": "volume", "RW": True},
            ]
        }
    ]
    with (
        patch.object(
            policy, "retained", side_effect=[sources, retained[:1], retained[1:]]
        ) as model,
        patch.object(fs, "read", side_effect=[b'"first"', b'"second"']) as read,
        patch.object(engine, "containers", return_value=containers),
        patch.object(policy, "paths") as paths,
        patch.object(engine, "inspect_sources") as inspect,
    ):
        engine.check_paths(candidate, RELEASE, missing=True)
    assert read.call_args_list == [
        call(directory / "a.json"),
        call(directory / "b.json"),
    ]
    assert model.call_args_list == [
        call(candidate),
        call("first"),
        call("second"),
    ]
    paths.assert_called_once_with(
        sources,
        retained,
        {Path("/candidate"), Path("/retained/b"), Path("/live")},
        engine.service,
        RELEASE,
    )
    inspect.assert_called_once_with(sources, missing=True)


def test_health_requires_exactly_one_matching_container(engine: Engine) -> None:
    with (
        patch.object(engine, "containers", return_value=[]),
        pytest.raises(CommandError, match=r"^health container count$"),
    ):
        engine.check_health(RELEASE)


def test_compose_binary_pins_match_linux_downloads() -> None:
    assert COMPOSE_SHA256 == {
        "amd64": "af426722a8af5de812fcee30cc1c39f0639f5f723cbadefb25e5d4db054064cf",
        "arm64": "f2f49f903240895fa764c0dca3a14548b754ad9426f6b609ccaf55ec5a3aa2a5",
    }


def test_compose_binary_digest_is_checked_after_descriptor_read(
    trusted_tree: Path,
) -> None:
    binary = trusted_tree / "compose"
    binary.write_bytes(b"pinned fixture")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with patch.object(compose, "COMPOSE_SHA256", {"amd64": digest}):
        compose.verify_binary(binary)
        binary.write_bytes(b"changed fixture")
        with pytest.raises(RefusalError, match=r"^Compose binary digest mismatch$"):
            compose.verify_binary(binary)
    link = trusted_tree / "link"
    link.symlink_to(binary)
    with pytest.raises(RefusalError, match=r"^symlink path$"):
        compose.verify_binary(link)
    binary.chmod(0o662)
    with pytest.raises(RefusalError, match=r"^writable control file$"):
        compose.verify_binary(binary)


def test_compose_and_probe_budgets_are_explicit(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    engine.run(RELEASE, "pull")
    assert fake.budgets[-1] == {
        "timeout_s": 300,
        "stdout_limit": 1024**2,
        "stderr_limit": 1024**2,
        "unit": "tbhb-deploy-o11y",
    }
    fake.inspected = healthy()
    with patch.object(compose.time, "monotonic", return_value=100):
        engine.check_health(RELEASE)
    assert fake.budgets[-1] == {
        "timeout_s": engine.service.timeout_s,
        "stdout_limit": 1024**2,
        "stderr_limit": 1024**2,
        "unit": "tbhb-deploy-o11y",
    }
    engine.sandbox(RELEASE, Path("/source"), Path("/env"), fixed=False)
    assert fake.budgets[-1] == {
        "timeout_s": 30,
        "stdout_limit": 1024**2,
        "stderr_limit": 1024**2,
        "unit": "tbhb-deploy-parse-o11y",
    }


@pytest.mark.parametrize("fixed", [False, True])
def test_exact_sandbox_argv(engine: Engine, fixed: bool) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    source, env = Path("/model.json"), Path("/vars.env")
    assert engine.sandbox(RELEASE, source, env, fixed=fixed) == fake.model
    directory = str(engine.service.root / "releases" / RELEASE)
    binds = "/compose " + directory + (" /model.json /dev/null" if fixed else "")
    assert fake.calls == [
        (
            [
                "/usr/bin/systemd-run",
                "--wait",
                "--pipe",
                "--collect",
                "--unit=tbhb-deploy-parse-o11y",
                "--property=TemporaryFileSystem=/:ro",
                "--property=MountAPIVFS=no",
                "--property=PrivateNetwork=yes",
                "--property=NoNewPrivileges=yes",
                "--property=CapabilityBoundingSet=",
                "--property=RuntimeMaxSec=30",
                "--property=BindReadOnlyPaths=" + binds,
                "/compose",
                "-p",
                "o11y",
                "--project-directory",
                directory,
                "-f",
                "/model.json",
                "--env-file",
                "/vars.env",
                "config",
                "--no-env-resolution",
                "--format",
                "json",
            ],
            None,
        )
    ]


@pytest.mark.parametrize("running", [b"", b"abc\n", b"invalid/id"])
def test_stop_has_daemon_timeout_and_kill_fallback(
    engine: Engine, running: bytes
) -> None:
    with (
        patch.object(
            engine, "command", side_effect=[b"abc", b"", running, b""]
        ) as command,
    ):
        if running == b"invalid/id":
            with pytest.raises(RefusalError, match=r"^container identifiers$"):
                engine.stop()
        else:
            engine.stop()
    expected = [
        call(
            [
                "/usr/bin/docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=o11y",
            ]
        ),
        call(["/usr/bin/docker", "stop", "-t", "10", "abc"]),
        call(
            [
                "/usr/bin/docker",
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=o11y",
            ]
        ),
    ]
    if running == b"abc\n":
        expected.append(call(["/usr/bin/docker", "kill", "abc"]))
    assert command.call_args_list == expected


def test_stop_failure_still_force_kills_running_containers(engine: Engine) -> None:
    with (
        patch.object(
            engine, "command", side_effect=[b"abc", CommandError, b"abc", b""]
        ) as command,
    ):
        engine.stop()
    assert command.call_args_list[-1] == call(["/usr/bin/docker", "kill", "abc"])


def test_oversized_inspect_cannot_block_stop(engine: Engine) -> None:
    calls: list[list[str]] = []

    def daemon(argv: list[str]) -> bytes:
        calls.append(argv)
        if argv[1] == "inspect":
            return Command()([sys.executable, "-c", "print('x' * (1024**2 + 1))"])
        if argv[1:3] == ["ps", "-aq"]:
            return b"abc"
        return b""

    with patch.object(engine, "command", side_effect=daemon):
        with pytest.raises(CommandError, match=r"^child stdout limit$"):
            engine.containers()
        calls.clear()
        engine.stop()
    assert calls == [
        [
            "/usr/bin/docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=o11y",
        ],
        ["/usr/bin/docker", "stop", "-t", "10", "abc"],
        [
            "/usr/bin/docker",
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=o11y",
        ],
    ]


def test_restart_ignores_oversized_failed_candidate_environment(engine: Engine) -> None:
    failed: Json = [{"Config": {"Env": ["TOKEN=" + "x" * OUTPUT_LIMIT]}}]
    assert len(json.dumps(failed).encode()) > OUTPUT_LIMIT
    fs.write(engine.model_path(RELEASE), json.dumps(minimal()).encode())
    inspections: list[Json] = []

    def containers() -> list[dict[str, Json]]:
        inspections.append(failed)
        message = "child stdout limit"
        raise CommandError(message)

    with (
        patch.object(engine, "containers", side_effect=containers),
        patch.object(engine, "health"),
    ):
        engine.up(RELEASE, recreate=True)
    assert inspections == []
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    assert "up" in fake.calls[-1][0]


@pytest.mark.parametrize("operation", ["deploy", "rollback", "recover"])
def test_tightened_policy_keeps_retained_release_usable(
    engine: Engine, operation: str
) -> None:
    old = minimal(image="registry.example/retired@sha256:" + "b" * 64)
    before = replace(
        engine.service, images=(*engine.service.images, "registry.example/retired")
    )
    assert policy.model(old, before) == ()
    with pytest.raises(RefusalError, match="unlisted image"):
        policy.model(old, engine.service)
    prior = "b" * 40
    (engine.service.root / "releases" / prior).mkdir()
    fs.write(engine.model_path(prior), json.dumps(old).encode())
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fake.inspected = [{**mapping(row), "Mounts": []} for row in array(healthy())]
    engine.service.secrets[0].target.parent.mkdir(parents=True)
    if operation == "deploy":
        engine.parse(RELEASE)
    elif operation == "rollback":
        engine.up(prior, recreate=True)
        assert any("--force-recreate" in argv for argv, _ in fake.calls)
    else:
        fs.mkdir(engine.service.root / "incoming")
        fs.pointer(engine.service.root / "current", "releases/" + prior)
        transaction = Transaction(engine.service, engine)
        transaction.save(RELEASE)
        assert transaction.recover()
        assert transaction.journal() is None
        assert fs.target(engine.service.root / "current") == "releases/" + prior


@pytest.mark.parametrize("present", [False, True])
def test_remove_only_existing_project_containers(engine: Engine, present: bool) -> None:
    command = engine.command
    assert isinstance(command, FakeCommand)
    command.inspected = [{}] if present else []
    engine.remove()
    assert command.calls == [
        (
            [
                "/usr/bin/docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=o11y",
            ],
            None,
        ),
        *(((["/usr/bin/docker", "rm", "-f", "abc"], None),) if present else ()),
    ]


@pytest.mark.parametrize("slow", ["list", "inspect", "probe", "second-probe"])
def test_health_deadline_limits_every_daemon_read_and_probe(
    engine: Engine, slow: str
) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fake.inspected = healthy()
    engine.service = replace(
        engine.service, timeout_s=10, probes=(("web", ("first",)), ("web", ("second",)))
    )
    now = 100.0
    budgets: list[float] = []

    def command(argv: list[str], data: bytes | None = None, **kwargs: object) -> bytes:
        nonlocal now
        if "ps" in argv:
            stage = "list"
        elif "inspect" in argv:
            stage = "inspect"
        else:
            stage = "second-probe" if argv[-1] == "second" else "probe"
        budget = float(str(kwargs.get("timeout_s", 60)))
        budgets.append(budget)
        cost = 20 if stage == slow else 2
        now += min(cost, budget)
        if cost > budget:
            message = "child command timeout"
            raise CommandError(message)
        assert isinstance(fake, FakeCommand)
        return fake(argv, data, **kwargs)

    with (
        patch.object(engine, "command", side_effect=command),
        patch.object(compose.time, "monotonic", side_effect=lambda: now),
        patch.object(
            compose.time,
            "sleep",
            side_effect=AssertionError("expired health gate retried"),
        ) as sleep,
        pytest.raises(CommandError, match=r"^health gate failed$"),
    ):
        engine.health(RELEASE)
    assert now == 110
    assert budgets == list(range(10, 10 - 2 * len(budgets), -2))
    sleep.assert_not_called()


@pytest.mark.parametrize("elapsed", [10, 11])
def test_health_cannot_accept_a_check_finishing_at_or_after_deadline(
    engine: Engine, elapsed: int
) -> None:
    now = 100

    def late_check(_release: str, *, deadline: float) -> None:
        nonlocal now
        assert deadline == 110
        now += elapsed

    with (
        patch.object(compose.time, "monotonic", side_effect=lambda: now),
        patch.object(engine, "check_health", side_effect=late_check),
        patch.object(
            compose.time,
            "sleep",
            side_effect=AssertionError("expired health gate retried"),
        ) as sleep,
        pytest.raises(CommandError, match=r"^health gate failed$"),
    ):
        engine.health(RELEASE)
    sleep.assert_not_called()


@pytest.mark.parametrize("deadline", [99, 100])
def test_expired_health_budget_refuses_before_querying_daemon(
    engine: Engine, deadline: float
) -> None:
    with (
        patch.object(compose.time, "monotonic", return_value=100),
        patch.object(engine, "command") as command,
        pytest.raises(CommandError, match=r"^health gate failed$"),
    ):
        engine.check_health(RELEASE, deadline=deadline)
    command.assert_not_called()


def test_health_propagates_unconfirmed_unit_stop(engine: Engine) -> None:
    def failed_probe(_release: str, *, deadline: float) -> None:
        assert deadline == 110
        Command().stop_unit("tbhb-deploy-o11y")

    with (
        patch.object(compose.time, "monotonic", side_effect=[100, 110]),
        patch.object(engine, "check_health", side_effect=failed_probe),
        patch.object(Command, "__call__", side_effect=CommandError),
        patch.object(compose.time, "sleep") as sleep,
        pytest.raises(RefusalError, match="compensation withheld"),
    ):
        engine.health(RELEASE)
    sleep.assert_not_called()


@pytest.mark.parametrize("missing", [None, "web", "worker"])
def test_local_images_inspects_every_required_digest(
    engine: Engine, missing: str | None
) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    services: Json = {
        name: {"image": "registry.example/" + name + "@sha256:" + "a" * 64}
        for name in ("web", "worker")
    }
    candidate: Json = {"services": services}
    fs.write(engine.model_path(RELEASE), json.dumps(candidate).encode())
    if missing is not None:
        fake.fail_argv = [
            "/usr/bin/docker",
            "image",
            "inspect",
            "registry.example/" + missing + "@sha256:" + "a" * 64,
        ]
    assert engine.local_images(RELEASE) is (missing is None)
    expected = [
        (
            [
                "/usr/bin/docker",
                "image",
                "inspect",
                "registry.example/" + name + "@sha256:" + "a" * 64,
            ],
            None,
        )
        for name in (("web",) if missing == "web" else ("web", "worker"))
    ]
    assert fake.calls == expected
