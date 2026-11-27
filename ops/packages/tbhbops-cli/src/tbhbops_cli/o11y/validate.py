"""Render a private release and validate it with isolated local tools."""

import argparse
import json
import stat
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import yaml

from tbhbops.o11y import validate
from tbhbops_cli.common import CliError
from tbhbops_cli.o11y import render
lazy from tbhbops_cli.o11y.filesystem import Filesystem
lazy from tbhbops_cli.o11y.runner import Result

if TYPE_CHECKING:
    import os

    from tbhbops_cli.cli import System

METADATA_TIMEOUT = 60
COMPOSE_TIMEOUT = 120
IMAGE_TIMEOUT = 900
COMPONENTS = ("loki", "tempo", "mimir", "alloy")


def static(fs: Filesystem, release: Path, configs: dict[str, Path]) -> list[str]:
    """Read raw YAML and all nested dashboard files through the filesystem seam."""
    try:
        base = yaml.safe_load(fs.read_text(release / "compose.yaml"))
        overlay = yaml.safe_load(fs.read_text(release / "compose.tools.yaml"))
        text = fs.read_text(configs["alloy"] / "config.alloy")
        grafana = configs["grafana"]
        provisioning = grafana / "provisioning"
        datasources = yaml.safe_load(
            fs.read_text(provisioning / "datasources/datasources.yaml")
        )
        dashboards = {
            name: content.decode()
            for name, content in render.tree_files(fs, grafana / "dashboards").items()
            if name.endswith(".json")
        }
        health = json.loads(dashboards["general/stack-health.json"])
        return [
            *validate.alloy(text),
            *validate.compose(base, overlay),
            *validate.dashboards(
                dashboards, {item["uid"] for item in datasources["datasources"]}
            ),
            *validate.alignment(
                base,
                text,
                health,
                fs.read_text(provisioning / "alerting/alerting.yaml"),
                fs.read_text(provisioning / "alerting/rules.yaml"),
            ),
        ]
    except OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError:
        return ["configuration tree could not be read or parsed"]


def _call(system: System, env: dict[str, str], argv: list[str], timeout: int) -> Result:
    return system.runner.run(["docker", *argv], env, timeout)


def local_engine(
    system: System, env: dict[str, str], *, dedicated: str | None = None
) -> str | None:
    try:
        return _local_details(system, env, dedicated=dedicated)
    except CliError as exc:
        return str(exc)


def _metadata(result: Result, message: str) -> str:
    if result.code is None:
        message = "timed out"
        raise CliError(message)
    if result.code != 0:
        raise CliError(message)
    return result.stdout.strip()


def _local_details(
    system: System, env: dict[str, str], *, dedicated: str | None = None
) -> str | None:
    if dedicated is not None:
        env["DOCKER_HOST"] = dedicated
    context = _call(system, env, ["context", "show"], METADATA_TIMEOUT)
    name = _metadata(context, "the Docker context could not be read")
    inspected = _call(
        system,
        env,
        [
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
            name,
        ],
        METADATA_TIMEOUT,
    )
    endpoint = _metadata(inspected, "the Docker context could not be inspected")
    info = system.filesystem.socket_lstat(Path(endpoint.removeprefix("unix://")))
    linux = system.platform == "linux"
    if dedicated is not None:
        problem = _dedicated_details(system, env, dedicated, name, endpoint, info)
        if problem is not None:
            return problem
    else:
        if not validate.local_context(
            name,
            endpoint,
            env.get("HOME"),
            linux=linux,
            socket=info is not None and stat.S_ISSOCK(info.st_mode),
        ):
            return "the Docker context does not use this machine's own daemon socket"
        identity = _call(
            system,
            env,
            ["info", "--format", "{{.Name}}|{{.OperatingSystem}}"],
            METADATA_TIMEOUT,
        )
        identity_text = _metadata(identity, "the engine is not this machine's own")
        if not validate.local_identity(
            identity_text, system.engine_hostname, linux=linux
        ):
            return "the engine is not this machine's own"
    version = _call(
        system,
        env,
        ["version", "--format", "{{.Server.MinAPIVersion}}"],
        METADATA_TIMEOUT,
    )
    pinned = validate.api_version(
        _metadata(version, "the Docker API version could not be read")
    )
    # Unrecognized values deliberately leave the API unpinned, as in pin_docker_api.
    if pinned is not None:
        env["DOCKER_API_VERSION"] = pinned
    return None


def _dedicated_details(  # noqa: PLR0913, PLR0917  # metadata from the same context inspection
    system: System,
    env: dict[str, str],
    named: str,
    context: str,
    endpoint: str,
    info: os.stat_result | None,
) -> str | None:
    contents = []
    for kind, argv in (
        ("containers", ["ps", "-aq"]),
        ("volumes", ["volume", "ls", "-q"]),
        ("networks", ["network", "ls", "--format", "{{.Name}}"]),
    ):
        result = _call(system, env, argv, METADATA_TIMEOUT)
        text = _metadata(result, f"the dedicated engine {kind} could not be listed")
        contents.append(text.splitlines())
    return validate.dedicated_engine(
        named,
        context,
        endpoint,
        socket=info is not None and stat.S_ISSOCK(info.st_mode),
        containers=contents[0],
        volumes=contents[1],
        networks=contents[2],
    )


def _fixtures(fs: Filesystem, target: Path) -> None:
    fs.mkdir(target)
    fs.chmod(target, 0o755)  # nosec B103: containers must traverse fixture directories
    for component in COMPONENTS:
        name = "config.alloy" if component == "alloy" else f"{component}.yaml"
        fs.write_atomic(
            target / name,
            files("tbhbops_cli.o11y.invalid").joinpath(name).read_bytes(),
            0o644,
        )


def validator_arguments(component: str, flag: str, *, fixture: bool) -> list[str]:
    name = "config.alloy" if component == "alloy" else f"{component}.yaml"
    paths = {
        True: f"/invalid/{name}",
        False: f"/etc/{component}/"
        + ("config.alloy" if component == "alloy" else "config.yaml"),
    }
    path = paths[fixture]
    return {
        "loki": [f"-config.file={path}", "-config.expand-env=true", "-verify-config"],
        "tempo": ["-target=all", f"-config.file={path}", "-config.verify=true"],
        "mimir": [f"-config.file={path}", "-config.expand-env=true", "-modules"],
        "alloy": ["validate", flag, path],
    }[component]


@dataclass(frozen=True)
class Images:
    system: System
    env: dict[str, str]
    configs: dict[str, Path]
    fixtures: Path
    services: dict[str, validate.Document]

    def run(self, component: str, args: list[str]) -> Result:
        return _call(
            self.system,
            self.env,
            [
                "run",
                "--rm",
                "--network",
                "none",
                "-e",
                "AWS_ACCESS_KEY_ID=validate",
                "-e",
                "AWS_SECRET_ACCESS_KEY=validate",
                "-v",
                f"{self.configs[component]}:/etc/{component}:ro",
                "-v",
                f"{self.fixtures}:/invalid:ro",
                self.services[component]["image"],
                *args,
            ],
            IMAGE_TIMEOUT,
        )

    def check(self, component: str, flag: str) -> str | None:
        valid = self.run(component, validator_arguments(component, flag, fixture=False))
        if valid.code != 0:
            return _real_problem(valid.code, "the real config was rejected")
        invalid = self.run(
            component, validator_arguments(component, flag, fixture=True)
        )
        return (
            "timed out"
            if invalid.code is None
            else validate.rejection(
                component, invalid.code, invalid.stdout + invalid.stderr
            )
        )


def _model(text: str) -> tuple[dict[str, validate.Document], str] | None:
    try:
        services = json.loads(text)["services"]
        flag = next(
            arg
            for arg in services["alloy"]["command"]
            if arg.startswith("--stability.level=")
        )
        images = [services[component]["image"] for component in COMPONENTS]
    except ValueError, KeyError, TypeError, StopIteration, AttributeError:
        return None
    if not all(isinstance(image, str) for image in images):
        return None
    return services, flag


def _problem(code: int | None, failure: str) -> str | None:
    if code is None:
        return "timed out"
    return None if code == 0 else failure


def _real_problem(code: int | None, failure: str) -> str | None:
    if code is not None:
        if code < 0:
            return "the real config check was killed by a signal"
        if code >= validate.DOCKER_LAUNCH_FAILURE:
            return f"the real config check never started (exit {code})"
    return _problem(code, failure)


def _checks(  # noqa: PLR0913, PLR0917  # release paths and process/output seams
    system: System,
    env: dict[str, str],
    release: Path,
    configs: dict[str, Path],
    fixtures: Path,
    out: TextIO,
) -> bool:
    passed = True

    def report(part: str, problem: str | None, success: str) -> None:
        nonlocal passed
        if problem is not None:
            passed = False
            out.write(f"FAIL {part}: {problem}\n")
        else:
            out.write(f"ok {part}: {success}\n")

    local = local_engine(system, env)
    report("local engine", local, "verified")
    if local is not None:
        return False
    prefix = ["compose", "--env-file", str(release / ".env.validate")]
    for name in ("compose.yaml", "compose.tools.yaml"):
        result = _call(
            system,
            env,
            [*prefix, "-f", str(release / name), "config", "-q"],
            COMPOSE_TIMEOUT,
        )
        problem = _problem(result.code, f"{name} does not load")
        report("compose model", problem, f"{name} loads")
    model = _call(
        system,
        env,
        [*prefix, "-f", str(release / "compose.yaml"), "config", "--format", "json"],
        COMPOSE_TIMEOUT,
    )
    if model.code != 0:
        problem = "timed out" if model.code is None else "could not read model"
        out.write(f"FAIL compose model: {problem}\n")
        return False
    parsed = _model(model.stdout)
    if parsed is None:
        out.write("FAIL compose model: missing images or Alloy --stability.level\n")
        return False
    services, flag = parsed
    images = Images(system, env, configs, fixtures, services)
    for component in COMPONENTS:
        report(
            component, images.check(component, flag), "valid accepted, invalid rejected"
        )
    default = images.run(
        "alloy",
        ["validate", "/etc/alloy/config.alloy"],
    )
    problem = (
        "timed out"
        if default.code is None
        else validate.stability(default.code, default.stdout + default.stderr, flag)
    )
    report(
        "alloy",
        problem,
        "otelcol.processor.deltatocumulative is the only component "
        "below generally available",
    )
    formatted = images.run(
        "alloy",
        ["fmt", "--test", "/etc/alloy/config.alloy"],
    )
    problem = _real_problem(formatted.code, "config.alloy is not formatted")
    report("alloy", problem, "formatted")
    return passed


def prepare(system: System, source: Path, temporary: Path, root_url: str) -> Path:
    """Render placeholder settings into a private temporary directory."""
    settings = {
        "O11Y_DATA_DIR": str(temporary / "data"),
        "O11Y_GRAFANA_ROOT_URL": root_url,
        "O11Y_GRAFANA_OWNER_LOGIN": "owner@example.invalid",
    }
    render.dispatch(
        argparse.Namespace(source=source, out=temporary, config_root=None),
        replace(system, env=settings),
    )
    return temporary / "release"


def dispatch(args: argparse.Namespace, system: System, out: TextIO) -> int:
    fs = system.filesystem
    try:
        temporary = fs.temporary_directory()
    except OSError as exc:
        out.write(
            f"FAIL validate: CreateTemporary directory failed: {type(exc).__name__}\n"
        )
        return 1
    step, item = "RenderRelease", "release"
    result = 1
    try:
        release = prepare(
            system, args.source, temporary, "https://grafana.example.example.invalid/"
        )
        step, item = "ReadEnvironment", "release/.env"
        values = {}
        for line in fs.read_text(release / ".env").splitlines():
            name, _, value = line.partition("=")
            values[name] = value
        step, item = "ReadConfigs", "release/.env"
        configs = {
            key.removeprefix("O11Y_CONFIG_").lower(): Path(value)
            for key, value in values.items()
            if key.startswith("O11Y_CONFIG_")
        }
        findings = static(fs, release, configs)
        out.writelines(f"FAIL static: {finding}\n" for finding in findings)
        if not findings:
            out.write("ok static: configuration checks pass\n")
            step, item = "PrepareFixtures", "invalid"
            fixtures = temporary / "invalid"
            _fixtures(fs, fixtures)
            step, item = "RunChecks", "docker"
            result = (
                0
                if _checks(
                    system,
                    validate.docker_environment(system.env),
                    release,
                    configs,
                    fixtures,
                    out,
                )
                else 1
            )
    except (OSError, ValueError, CliError) as exc:
        out.write(f"FAIL validate: {step} {item} failed: {type(exc).__name__}\n")
    finally:
        try:
            render.remove_tree(fs, temporary)
        except Exception:  # noqa: BLE001  # cleanup must not replace the command result
            out.write(
                f"FAIL validate: temporary directory {str(temporary)!r} "
                "could not be removed\n"
            )
            result = 1
    return result
