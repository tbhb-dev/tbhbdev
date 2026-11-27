"""Hold smoke run state and execute commands through system seams."""

# pylint: disable=duplicate-code  # independent protocol contracts

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbops.o11y import rustfs_probe, smoke, validate
lazy from tbhbops.o11y.validate import Document

from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke.safety import (
    require,
    safe_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from tbhbops_cli.cli import System
    from tbhbops_cli.o11y.filesystem import Filesystem

CONTROL_TIMEOUT = 120
PROBE_CURL_TIMEOUT = 10
CURL_EXEC_MARGIN = 5
OBJECT_TIMEOUT = 900


def environment(
    inherited: Mapping[str, str], data: Path, owner: tuple[int, int]
) -> dict[str, str]:
    env = validate.docker_environment(inherited)
    # Ignore context overrides as well as DOCKER_HOST before verifying the engine.
    env.pop("DOCKER_CONTEXT", None)
    return {
        **env,
        **smoke.FIXED_ENV,
        "O11Y_DATA_DIR": str(data),
        "O11Y_SECRETS_OWNER": f"{owner[0]}:{owner[1]}",
    }


@dataclass
class Smoke:
    system: System
    out: Path
    artifacts: Path
    host: Path
    data: Path
    project: str
    sentinels: smoke.Sentinels
    env: dict[str, str]
    image: str = ""
    model: dict[str, Document] = field(default_factory=dict)
    started: bool = False
    transcript: list[str] = field(default_factory=list)
    finalized: bool = False
    output_ready: bool = False
    current_values: dict[str, bytes] = field(default_factory=dict)
    release: Path | None = None
    dedicated: str | None = None
    group: str = "before group 1"
    step: int = 1

    @property
    def fs(self) -> Filesystem:
        return self.system.filesystem

    @property
    def linux(self) -> bool:
        return self.system.platform == "linux"

    def emit(self, line: str) -> None:
        self.transcript.append(line + "\n")
        self.finalized = False

    def finalize_transcript(self) -> None:
        if not self.finalized:
            self.fs.write_atomic(
                self.out / "transcript.log", "".join(self.transcript), 0o600
            )
            self.finalized = True

    def stage[**P](
        self,
        group: str,
        operation: Callable[P, None],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        self.group = group
        self.step = 1
        operation(*args, **kwargs)

    def run(
        self,
        argv: list[str],
        timeout: int = CONTROL_TIMEOUT,
        *,
        input_data: str | None = None,
    ) -> Result:
        result = self.system.runner.run(argv, self.env, timeout, input_data=input_data)
        require(
            len(result.stdout.encode(errors="surrogateescape"))
            + len(result.stderr.encode(errors="surrogateescape"))
            <= smoke.LIMIT,
            smoke.FAILURES["output_exceeds_4_mib"],
        )
        # Number command boundaries, never command arguments. In-process work
        # before the next command belongs to that next step as well.
        self.step += 1
        return result

    def compose(  # explicit Compose modes and execution budgets
        self,
        args: list[str],
        *,
        tools: bool = False,
        dummy: bool = False,
        fixture: bool = False,
        timeout: int = CONTROL_TIMEOUT,
        head: bool = False,
        input_data: str | None = None,
    ) -> Result:
        release = self.release or self.out / "release"
        argv = [
            "docker",
            "compose",
            "-p",
            self.project,
            "--env-file",
            str(release / (".env.validate" if dummy else ".env")),
            "-f",
            str(release / "compose.yaml"),
        ]
        if tools:
            argv.extend(
                ["-f", str(release / "compose.tools.yaml"), "--profile", "tools"]
            )
        if fixture:
            argv.extend(["-f", str(self.out / "readiness-fixture.yaml")])
        if head:
            return self.system.runner.read_head(
                [*argv, *args], self.env, timeout, smoke.LIMIT, 200
            )
        return self.run([*argv, *args], timeout, input_data=input_data)

    def utility(
        self, args: list[str], *, host: bool = False, source: Path | None = None
    ) -> Result:
        # The path is checked before *every* root operation, including cleanup.
        if self.linux:
            require(
                self.data == self.host and self.data.parent != self.data,
                smoke.FAILURES["unsafe_data_dir"],
            )
            safe_path(self.fs, self.data)
        else:
            require(
                self.data
                == Path(rustfs_probe.VM_DATA_ROOT) / self.project.removeprefix("o11y-"),
                smoke.FAILURES["unsafe_data_dir"],
            )
            for path in ("/parent/o11y-test", f"/parent/o11y-test/{self.data.name}"):
                check = self.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--log-driver",
                        "none",
                        "--user",
                        "0:0",
                        "--network",
                        "none",
                        "-v",
                        "/var/lib:/parent:ro",
                        "--entrypoint",
                        "test",
                        self.image,
                        "-L",
                        path,
                    ]
                )
                require(
                    check.code == 1,
                    smoke.FAILURES["data_dir_symlink_or_inspection_failure"],
                )
        mount = self.data if self.linux else self.data.parent
        argv = [
            "docker",
            "run",
            "--rm",
            "--log-driver",
            "none",
            "--user",
            "0:0",
            "--network",
            "none",
            "-v",
            f"{mount}:/d",
        ]
        if host:
            argv.extend(["-v", f"{self.host}:/host"])
        if source is not None:
            argv.extend(["-v", f"{source}:/src:ro"])
        if not self.linux:
            # Inspect the child of the VM mount; Linux uses the host lstat above.
            check = self.run(
                [*argv, "--entrypoint", "test", self.image, "-L", self.destination]
            )
            require(
                check.code == 1,
                smoke.FAILURES["data_dir_symlink_or_inspection_failure"],
            )
        return self.run([*argv, "--entrypoint", args[0], self.image, *args[1:]])

    @property
    def destination(self) -> str:
        return "/d" if self.linux else f"/d/{self.data.name}"

    def logged(
        self,
        args: list[str],
        name: str,
        *,
        tools: bool = False,
        fixture: bool = False,
        timeout: int = CONTROL_TIMEOUT,
    ) -> Result:
        result = self.compose(args, tools=tools, fixture=fixture, timeout=timeout)
        self.fs.write_atomic(
            self.out / name,
            (result.stdout + result.stderr).encode(errors="surrogateescape"),
            0o600,
        )
        return result

    def probe(self, args: list[str]) -> Result:
        return self.compose(
            ["run", "--rm", "-T", "s3probe", *args], tools=True, timeout=OBJECT_TIMEOUT
        )

    def inspect(
        self,
        service: str,
        template: str,
        *,
        fixture: bool = False,
        all_containers: bool = False,
    ) -> Result:
        container = self.compose(
            ["ps", *(["-a"] if fixture or all_containers else []), "-q", service],
            fixture=fixture,
        )
        if container.code != 0 or not container.stdout.strip():
            return Result(1)
        return self.run(["docker", "inspect", "-f", template, container.stdout.strip()])

    def disk_counts(self) -> str:
        result = self.utility(["stat", "-f", "-c", "%b %f", self.destination + "/loki"])
        return result.stdout if result.code == 0 else ""

    def record_loki_disk(self, phase: str) -> None:
        self.emit(smoke.disk_record(self.disk_counts(), phase))

    def inside(
        self,
        url: str,
        *,
        body: bool = False,
        args: tuple[str, ...] = (),
        input_data: str | None = None,
        curl_timeout: int = PROBE_CURL_TIMEOUT,
    ) -> Result:
        flags = (
            ["-w", "\n%{http_code}"]
            if body
            else ["-o", "/dev/null", "-w", "%{http_code}"]
        )
        return self.compose(
            [
                "exec",
                "-T",
                "rustfs",
                "curl",
                "-s",
                *flags,
                "-m",
                str(curl_timeout),
                *args,
                url,
            ],
            timeout=curl_timeout + CURL_EXEC_MARGIN,
            input_data=input_data,
        )

    def container_ids(self) -> dict[str, tuple[str, str]]:
        states = {}
        for service in smoke.SERVICES:
            result = self.inspect(service, "{{.Id}} {{.State.StartedAt}}")
            require(result.code == 0, smoke.FAILURES["container_state_unavailable"])
            container, timestamp = result.stdout.split()
            states[service] = container, timestamp
        return states
