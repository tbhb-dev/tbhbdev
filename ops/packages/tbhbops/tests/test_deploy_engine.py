"""The engine parses in empty units and starts only stored, rechecked models."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, TypedDict, Unpack, override
from unittest.mock import Mock, call, patch

import pytest
from tbhbops.deploy import engine as engine_module
from tbhbops.deploy import filesystem as fs
from tbhbops.deploy import policy
from tbhbops.deploy.engine import COMPOSE_SHA256, Command, CommandError, Engine
from tbhbops.deploy.protocol import Json, RefusalError, array, mapping
from tbhbops.deploy.transaction import Transaction
lazy from tbhbops.deploy.config import Service

from .deploy_model import minimal
from .test_deploy_config import deploy_service as deploy_service  # noqa: PLC0414  # pytest fixture registration
from .test_deploy_filesystem import trusted_tree as trusted_tree  # noqa: PLC0414  # pytest fixture registration

RELEASE = "a" * 40


class FakeCommand(Command):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.budgets: list[dict[str, object]] = []
        self.model: Json = minimal()
        self.fixed: Json = None
        self.inspected: Json = []
        self.names = b""
        self.resources: Json = []
        self.active = False
        self.stopped_units: set[str] = set()
        self.failed = False
        self.fail_argv: list[str] = []
        self.info = json.dumps(
            {
                "Driver": "overlayfs",
                "ServerVersion": "29.9.3",
                "PidsLimit": True,
                "MemoryLimit": True,
                "SwapLimit": True,
            }
        ).encode()
        self.info_error: Exception | None = None

    @override
    def __call__(
        self, argv: list[str], data: bytes | None = None, **kwargs: object
    ) -> bytes:
        self.calls.append((argv, data))
        self.budgets.append(kwargs)
        if self.failed or argv == self.fail_argv:
            raise CommandError
        if argv[:2] == ["/usr/bin/docker", "info"]:
            return self.daemon_info()
        if argv[0] == "/usr/bin/systemctl":
            return self.systemctl(argv)
        if "config" in argv:
            value = (
                self.fixed
                if self.fixed is not None
                and "--env-file" in argv
                and argv[argv.index("--env-file") + 1] == "/dev/null"
                else self.model
            )
            return json.dumps(value).encode()
        if argv[:2] == ["/usr/bin/docker", "inspect"]:
            return json.dumps(self.inspected).encode()
        if "ls" in argv or "inspect" in argv:
            return self.names if "ls" in argv else json.dumps(self.resources).encode()
        return (
            b"abc"
            if argv[:3] == ["/usr/bin/docker", "ps", "-aq"] and self.inspected
            else b""
        )

    def daemon_info(self) -> bytes:
        if self.info_error is not None:
            raise self.info_error
        return self.info

    def systemctl(self, argv: list[str]) -> bytes:
        if argv[1] == "stop":
            self.stopped_units.add(argv[-1])
        return (
            b"active\n"
            if self.active and argv[-1] not in self.stopped_units
            else b"inactive\n"
        )


class ChildOptions(TypedDict, total=False, closed=True):
    stdin: BinaryIO
    stdout: int
    stderr: int
    env: dict[str, str]
    start_new_session: bool


@pytest.fixture(autouse=True)
def real_command_children_have_private_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen

    def spawn(
        argv: list[str], **options: Unpack[ChildOptions]
    ) -> subprocess.Popen[bytes]:
        child = original(argv, **options)
        if sys.platform != "win32":
            try:
                group = os.getpgid(child.pid)
            except ProcessLookupError:
                # A short-lived child may exit before the parent checks it.
                assert child.poll() is not None
            else:
                if group != child.pid:
                    # Kill the individual child before a bad group can hang a cap
                    # or timeout test. The test verifies the real kernel group.
                    child.kill()
                    child.communicate()
                    pytest.fail("command child has no private process group")
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)


@pytest.fixture
def engine(deploy_service: Service, trusted_tree: Path) -> Engine:
    secret = replace(
        deploy_service.secrets[0], target=trusted_tree / "data" / "secrets" / "token"
    )
    service = replace(
        deploy_service,
        root=trusted_tree,
        secrets=(secret,),
        ro_binds=(
            trusted_tree / "data" / "web" / "config",
            trusted_tree / "data" / "secrets" / "generated",
        ),
    )
    for path in (
        trusted_tree / "deploy",
        trusted_tree / "deploy" / "models",
        trusted_tree / "releases",
        trusted_tree / "releases" / RELEASE,
    ):
        path.mkdir(mode=0o700)
    return Engine(service, "/compose", FakeCommand())


def test_sandbox_parse_fixed_point_and_run(engine: Engine) -> None:
    engine.parse(RELEASE)
    assert engine.model_path(RELEASE).parent == engine.state / "models"
    assert (engine.model_path(RELEASE).stat().st_mode & 0o777) == 0o600
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    reads = [call for call, _ in fake.calls if "config" in call]
    assert len(reads) == 2
    assert all("--property=TemporaryFileSystem=/:ro" in call for call in reads)
    assert all("--property=MountAPIVFS=no" in call for call in reads)
    assert all("--property=PrivateNetwork=yes" in call for call in reads)
    assert all("--property=NoNewPrivileges=yes" in call for call in reads)
    assert all("--property=CapabilityBoundingSet=" in call for call in reads)
    assert all("--no-env-resolution" in call for call in reads)
    assert str(engine.model_path(RELEASE)) in reads[1]
    assert " /dev/null" in next(
        argument for argument in reads[1] if "BindReadOnlyPaths" in argument
    )
    engine.run(RELEASE, "pull")
    argv = fake.calls[-1][0]
    assert argv[:5] == [
        "/usr/bin/systemd-run",
        "--unit=tbhb-deploy-o11y",
        "--collect",
        "--wait",
        "--pipe",
    ]
    assert not any(argument.startswith("--property") for argument in argv)
    assert "down" not in argv
    assert str(engine.model_path(RELEASE)) in argv
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


def test_capabilities_names_the_service_requiring_the_limit(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    info = json.loads(fake.info)
    info["SwapLimit"] = False
    fake.info = json.dumps(info).encode()
    candidate: Json = {
        "services": {
            "web": {"pids_limit": 10, "mem_limit": 1024},
            "worker": {"memswap_limit": 2048},
        }
    }
    with pytest.raises(
        RefusalError, match=r"^daemon cannot enforce SwapLimit for service worker$"
    ):
        engine.capabilities(candidate)


def test_capabilities_requires_only_limits_present_in_services(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    fake.info = (
        b'{"Driver":"overlayfs","ServerVersion":"29.9.3",'
        b'"PidsLimit":false,"MemoryLimit":false,"SwapLimit":false}'
    )
    engine.capabilities({"services": {"web": {}}})


def test_stop_uses_ids_and_stops_active_unit(engine: Engine) -> None:
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    engine.stop()
    engine.stop_unit()
    fake.active = True
    engine.stop_unit()
    assert fake.calls[-1][0] == [
        "/usr/bin/systemctl",
        "show",
        "--property=ActiveState",
        "--value",
        "tbhb-deploy-parse-o11y",
    ]
    fake.inspected = [{"Id": "abc", "Mounts": []}]
    engine.stop()
    assert fake.calls[-1][0] == [
        "/usr/bin/docker",
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=o11y",
    ]
    assert any(
        "label=com.docker.compose.project=o11y" in argv for argv, _ in fake.calls
    )
    fake.inspected = ["invalid"]
    with pytest.raises(RefusalError):
        engine.containers()


def test_resource_ownership(engine: Engine) -> None:
    candidate: Json = {
        "networks": {"default": {"name": "o11y_default"}},
        "volumes": {"cache": {"name": "o11y_cache"}},
    }
    fake = engine.command
    assert isinstance(fake, FakeCommand)
    engine.ownership(candidate)
    fake.names = b"o11y_default\no11y_cache\n"
    fake.resources = [{"Labels": {"com.docker.compose.project": "o11y"}}]
    engine.ownership(candidate)
    fake.resources = [{"Labels": {"com.docker.compose.project": "foreign"}}]
    with pytest.raises(RefusalError, match=r"^foreign resource ownership$"):
        engine.ownership(candidate)
    fake.resources = []
    with pytest.raises(RefusalError, match=r"^resource inspect type$"):
        engine.ownership(candidate)


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
    monkeypatch.setattr("tbhbops.deploy.engine.time.monotonic", lambda: now)
    calls = 0

    def eventually_healthy(seconds: float) -> None:
        nonlocal calls, now
        calls += 1
        now += seconds
        fake.inspected = healthy()

    monkeypatch.setattr("tbhbops.deploy.engine.time.sleep", eventually_healthy)
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


def test_command_empty_environment() -> None:
    script = "import os,sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
        assert (
            Command()([sys.executable, "-S", "-c", script], b"input", timeout_s=1)
            == b"input"
        )
    assert popen.call_args.kwargs["env"] == {}
    assert popen.call_args.kwargs["start_new_session"] is True
    assert Command()(["/usr/bin/env"], timeout_s=1) == b""
    with pytest.raises(CommandError, match=r"^child command failed$"):
        Command()([sys.executable, "-c", "import sys; sys.exit(1)"], timeout_s=1)


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
            "tbhbops.deploy.engine.time.monotonic",
            side_effect=[100, 100 + elapsed, 100 + elapsed],
        ),
        patch("tbhbops.deploy.engine.time.sleep") as sleep,
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


def test_resource_ownership_exact_queries(engine: Engine) -> None:
    candidate: Json = {
        "networks": {"a": {"name": "o11y_a"}},
        "volumes": {"b": {"name": "o11y_b"}},
    }
    with patch.object(
        engine,
        "command",
        side_effect=[
            b"o11y_a\n",
            b'[{"Labels":{"com.docker.compose.project":"o11y"}}]',
            b"o11y_b\n",
            b'[{"Labels":{"com.docker.compose.project":"o11y"}}]',
        ],
    ) as command:
        engine.ownership(candidate)
    assert command.call_args_list == [
        call(["/usr/bin/docker", "network", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "network", "inspect", "o11y_a"]),
        call(["/usr/bin/docker", "volume", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "volume", "inspect", "o11y_b"]),
    ]


def test_parse_checks_and_persists_the_fixed_model_in_order(engine: Engine) -> None:
    candidate: Json = {"z": 1, "a": 2}
    with (
        patch.object(engine, "sandbox", return_value=candidate) as sandbox,
        patch.object(policy, "model") as model,
        patch.object(engine, "check_paths") as paths,
        patch.object(fs, "write") as write,
        patch.object(engine, "ownership") as ownership,
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
        call.ownership(candidate),
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


def test_resource_listing_continues_after_an_absent_resource(engine: Engine) -> None:
    candidate: Json = {
        "networks": {
            "absent": {"name": "o11y_absent"},
            "present": {"name": "o11y_present"},
        }
    }
    with patch.object(
        engine,
        "command",
        side_effect=[
            b"o11y_present\n",
            b"o11y_present\n",
            b'[{"Labels":{"com.docker.compose.project":"o11y"}}]',
        ],
    ) as command:
        engine.ownership(candidate)
    assert command.call_args_list == [
        call(["/usr/bin/docker", "network", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "network", "ls", "--format", "{{.Name}}"]),
        call(["/usr/bin/docker", "network", "inspect", "o11y_present"]),
    ]


def test_resource_without_labels_is_foreign(engine: Engine) -> None:
    with (
        patch.object(engine, "command", side_effect=[b"o11y_a", b"[{}]"]),
        pytest.raises(RefusalError, match=r"^foreign resource ownership$"),
    ):
        engine.ownership({"networks": {"a": {"name": "o11y_a"}}})


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
    with patch.object(engine_module, "COMPOSE_SHA256", {"amd64": digest}):
        engine_module.verify_binary(binary)
        binary.write_bytes(b"changed fixture")
        with pytest.raises(RefusalError, match=r"^Compose binary digest mismatch$"):
            engine_module.verify_binary(binary)
    link = trusted_tree / "link"
    link.symlink_to(binary)
    with pytest.raises(RefusalError, match=r"^symlink path$"):
        engine_module.verify_binary(link)
    binary.chmod(0o662)
    with pytest.raises(RefusalError, match=r"^writable control file$"):
        engine_module.verify_binary(binary)


@pytest.mark.parametrize("descriptor", [1, 2])
def test_command_caps_each_stream(descriptor: int) -> None:
    script = f"import os;\nwhile True: os.write({descriptor}, b'x' * 4096)"
    reason = "child stdout limit" if descriptor == 1 else "child stderr limit"
    started = time.monotonic()
    with pytest.raises(CommandError, match="^" + reason + "$"):
        Command()(
            [sys.executable, "-c", script],
            timeout_s=1,
            stdout_limit=1024,
            stderr_limit=2048,
        )
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("closed", [False, True])
def test_command_deadline_also_applies_after_output_closes(closed: bool) -> None:
    script = (
        "import os; os.close(1); os.close(2); " if closed else ""
    ) + "import time; time.sleep(10)"
    started = time.monotonic()
    with pytest.raises(CommandError, match=r"^child command timeout$"):
        Command()([sys.executable, "-c", script], timeout_s=0.1)
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("budget", ["cap", "timeout"])
def test_command_budget_stops_transient_unit(budget: str) -> None:
    stops: list[tuple[list[str], dict[str, object]]] = []

    class RecordingCommand(Command):
        @override
        def __call__(
            self,
            argv: list[str],
            data: bytes | None = None,
            *,
            timeout_s: float = 60,
            stdout_limit: int = 1024**2,
            stderr_limit: int = 1024**2,
            unit: str | None = None,
        ) -> bytes:
            if argv[0] == "/usr/bin/systemctl":
                stops.append(
                    (
                        argv,
                        {
                            "timeout_s": timeout_s,
                            "stdout_limit": stdout_limit,
                            "stderr_limit": stderr_limit,
                        },
                    )
                )
                return b"active\n" if len(stops) == 1 else b"inactive\n"
            return super().__call__(
                argv,
                data,
                timeout_s=timeout_s,
                stdout_limit=stdout_limit,
                stderr_limit=stderr_limit,
                unit=unit,
            )

    reason = "child stdout limit" if budget == "cap" else "child command timeout"
    script = (
        "print('too much')"
        if budget == "cap"
        else "import os,time; os.close(1); os.close(2); time.sleep(10)"
    )
    with pytest.raises(CommandError, match="^" + reason + "$"):
        RecordingCommand()(
            [sys.executable, "-c", script],
            stdout_limit=1,
            timeout_s=1 if budget == "cap" else 0.1,
            unit="tbhb-deploy-o11y",
        )
    assert stops == [
        (argv, {"timeout_s": 10, "stdout_limit": 1024**2, "stderr_limit": 1024**2})
        for argv in [
            [
                "/usr/bin/systemctl",
                "show",
                "--property=ActiveState",
                "--value",
                "tbhb-deploy-o11y",
            ],
            ["/usr/bin/systemctl", "stop", "tbhb-deploy-o11y"],
            [
                "/usr/bin/systemctl",
                "show",
                "--property=ActiveState",
                "--value",
                "tbhb-deploy-o11y",
            ],
        ]
    ]


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
    with patch.object(engine_module.time, "monotonic", return_value=100):
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


def test_command_default_budgets_and_remaining_wait() -> None:
    with (
        patch.object(engine_module.time, "monotonic", return_value=100),
        patch.object(engine_module, "capture", return_value=b"result") as capture,
        patch.object(subprocess, "Popen") as popen,
    ):
        child = popen.return_value.__enter__.return_value
        child.returncode = 0
        assert Command()(["/command"]) == b"result"
    assert capture.call_args.args == (child.stdout, child.stderr, 160, 1024**2, 1024**2)
    child.wait.assert_called_once_with(timeout=60)
    assert popen.call_args.kwargs["stdout"] == subprocess.PIPE
    assert popen.call_args.kwargs["stderr"] == subprocess.PIPE
    assert popen.call_args.kwargs["start_new_session"] is True


def test_command_empty_stdin_and_independent_stream_boundaries() -> None:
    echo = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    ]
    assert Command()(echo, timeout_s=1) == b""
    assert Command()(echo, b"", timeout_s=1) == b""
    script = "import os; os.write(1,b'ab'); os.write(2,b'data')"
    assert (
        Command()(
            [sys.executable, "-c", script], stdout_limit=2, stderr_limit=4, timeout_s=1
        )
        == b"ab"
    )
    for stdout_limit, stderr_limit, reason in [
        (1, 4, "child stdout limit"),
        (2, 3, "child stderr limit"),
    ]:
        with pytest.raises(CommandError, match="^" + reason + "$"):
            Command()(
                [sys.executable, "-c", script],
                stdout_limit=stdout_limit,
                stderr_limit=stderr_limit,
                timeout_s=1,
            )


def test_read_output_never_requests_more_than_remaining_cap_and_one_byte() -> None:
    output = bytearray(b"ab")
    with patch.object(engine_module.os, "read", side_effect=[b"cd", b"e"]) as read:
        assert engine_module.read_output(11, output, 4, stdout=True)
        assert output == b"abcd"
        with pytest.raises(CommandError, match=r"^child stdout limit$"):
            engine_module.read_output(11, output, 4, stdout=True)
    assert read.call_args_list == [call(11, 3), call(11, 1)]
    assert output == b"abcd"


def test_capture_refuses_at_deadline_even_if_output_is_ready() -> None:
    stdout, stderr = Mock(), Mock()
    stdout.fileno.return_value, stderr.fileno.return_value = 11, 12
    with (
        patch.object(engine_module.time, "monotonic", return_value=100),
        patch.object(engine_module.selectors, "DefaultSelector") as selector,
    ):
        selected = selector.return_value.__enter__.return_value
        closed: dict[int, object] = {}
        selected.get_map = Mock(side_effect=[{11: object()}, closed])
        selected.select = Mock(
            side_effect=AssertionError("expired deadline polled output")
        )
        with pytest.raises(CommandError, match=r"^child command timeout$"):
            engine_module.capture(stdout, stderr, 100, 10, 10)
    selected.select.assert_not_called()


@pytest.mark.parametrize("failure", [CommandError, OSError])
def test_unit_stop_failure_blocks_compensation(
    failure: type[Exception],
) -> None:
    child = Mock(pid=123)
    command = Command()
    with (
        patch.object(engine_module.os, "killpg") as kill,
        patch.object(Command, "__call__", side_effect=failure) as stop,
        pytest.raises(
            RefusalError,
            match=r"^transient unit stop unconfirmed; compensation withheld$",
        ),
    ):
        command.terminate(child, "tbhb-deploy-o11y")
    if sys.platform != "win32":
        kill.assert_called_once_with(123, signal.SIGKILL)
    child.wait.assert_called_once_with()
    stop.assert_called_once_with(
        [
            "/usr/bin/systemctl",
            "show",
            "--property=ActiveState",
            "--value",
            "tbhb-deploy-o11y",
        ],
        timeout_s=10,
    )


def test_read_output_reports_eof_without_extending_output() -> None:
    output = bytearray(b"before")
    with patch.object(engine_module.os, "read", return_value=b""):
        assert engine_module.read_output(11, output, 10, stdout=True) is False
    assert output == b"before"


def test_command_wait_never_extends_an_expired_budget() -> None:
    with (
        patch.object(engine_module.time, "monotonic", side_effect=[100, 161]),
        patch.object(engine_module, "capture", return_value=b"result"),
        patch.object(subprocess, "Popen") as popen,
    ):
        child = popen.return_value.__enter__.return_value
        child.returncode = 0
        assert Command()(["/command"]) == b"result"
    child.wait.assert_called_once_with(timeout=0)


def test_terminate_still_stops_unit_when_the_child_has_exited() -> None:
    child = Mock(pid=123)
    with (
        patch.object(engine_module.os, "killpg", side_effect=ProcessLookupError),
        patch.object(Command, "__call__", return_value=b"inactive\n") as stop,
    ):
        Command().terminate(child, "tbhb-deploy-o11y")
    child.wait.assert_called_once_with()
    stop.assert_called_once_with(
        [
            "/usr/bin/systemctl",
            "show",
            "--property=ActiveState",
            "--value",
            "tbhb-deploy-o11y",
        ],
        timeout_s=10,
    )


@pytest.mark.parametrize("open_stream", [False, True])
def test_capture_unregisters_only_streams_that_reached_eof(open_stream: bool) -> None:
    stdout, stderr = Mock(), Mock()
    stdout.fileno.return_value, stderr.fileno.return_value = 11, 12
    key = Mock(fd=11)
    with (
        patch.object(engine_module.time, "monotonic", return_value=100),
        patch.object(engine_module.selectors, "DefaultSelector") as selector,
        patch.object(engine_module, "read_output", return_value=open_stream),
    ):
        selected = selector.return_value.__enter__.return_value
        closed: dict[int, object] = {}
        selected.get_map = Mock(side_effect=[{11: object()}, closed])
        selected.select.return_value = [(key, 1)]
        assert engine_module.capture(stdout, stderr, 101, 10, 10) == b""
    if open_stream:
        selected.unregister.assert_not_called()
    else:
        selected.unregister.assert_called_once_with(11)


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


@pytest.mark.parametrize(
    ("outputs", "reason"),
    [
        ([CommandError], "transient unit stop unconfirmed; compensation withheld"),
        ([OSError], "transient unit stop unconfirmed; compensation withheld"),
        (
            [b"active\n", CommandError],
            "transient unit stop unconfirmed; compensation withheld",
        ),
        (
            [b"active\n", b"", OSError],
            "transient unit stop unconfirmed; compensation withheld",
        ),
        (
            [b"active\n", b"", b"active\n"],
            "transient unit is not inactive; compensation withheld",
        ),
        (
            [b"active\n", b"", b"deactivating\n"],
            "transient unit is not inactive; compensation withheld",
        ),
        (
            [b"active\n", b"", b""],
            "transient unit is not inactive; compensation withheld",
        ),
    ],
)
def test_unit_stop_requires_confirmed_inactivity(
    outputs: list[bytes | type[Exception]], reason: str
) -> None:
    with (
        patch.object(Command, "__call__", side_effect=outputs),
        pytest.raises(RefusalError) as caught,
    ):
        Command().stop_unit("tbhb-deploy-o11y")
    assert str(caught.value) == reason


@pytest.mark.parametrize("active", [False, True])
def test_unit_stop_waits_with_bounded_commands(active: bool) -> None:
    unit = "tbhb-deploy-o11y"
    query = call(
        ["/usr/bin/systemctl", "show", "--property=ActiveState", "--value", unit],
        timeout_s=10,
    )
    outputs = [b"active\n", b"", b"inactive\n"] if active else [b"inactive\n"]
    with patch.object(Command, "__call__", side_effect=outputs) as command:
        Command().stop_unit(unit)
    expected = [query]
    if active:
        expected.extend(
            [
                call(["/usr/bin/systemctl", "stop", unit], timeout_s=10),
                query,
            ]
        )
    assert command.call_args_list == expected


def test_restart_ignores_oversized_failed_candidate_environment(engine: Engine) -> None:
    failed: Json = [{"Config": {"Env": ["TOKEN=" + "x" * engine_module.OUTPUT_LIMIT]}}]
    assert len(json.dumps(failed).encode()) > engine_module.OUTPUT_LIMIT
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
        patch.object(engine_module.time, "monotonic", side_effect=lambda: now),
        patch.object(
            engine_module.time,
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
        patch.object(engine_module.time, "monotonic", side_effect=lambda: now),
        patch.object(engine, "check_health", side_effect=late_check),
        patch.object(
            engine_module.time,
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
        patch.object(engine_module.time, "monotonic", return_value=100),
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
        patch.object(engine_module.time, "monotonic", side_effect=[100, 110]),
        patch.object(engine, "check_health", side_effect=failed_probe),
        patch.object(Command, "__call__", side_effect=CommandError),
        patch.object(engine_module.time, "sleep") as sleep,
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
