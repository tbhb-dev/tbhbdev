"""Scripted command and admin seams for the extended smoke stack."""

# pylint: disable=duplicate-code  # verify protocol literals independently
# cspell:ignore udcff udcfe

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
lazy from collections.abc import Mapping, Sequence

import yaml
from tbhbops.o11y import smoke
from tbhbops.o11y import smoke_capacity as capacity
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    alerts,
    runtime,
)
from test_o11y_cli import Fake
from unit.o11y.smoke.grafana_fixture import EngineB, instance_b
from unit.o11y.smoke.metrics_fixture import ROOT, HttpC
from unit.o11y.smoke.runtime_fixture import OUT

SOURCE = Path("/stack")


@dataclass
class EngineC(EngineB):
    calls: list[tuple[list[str], dict[str, str], int]] = field(default_factory=list)
    overrides: dict[str, Result] = field(default_factory=dict)
    upload: str = "{}\n200"

    states: dict[str, tuple[str, str]] = field(
        default_factory=lambda: {s: (s, "2026-10-25T05:38:26Z") for s in smoke.SERVICES}
    )
    stopped: set[str] = field(default_factory=set)
    owner_time: int = 0
    reset_uploaded: bool = False
    uploaded: list[str] = field(default_factory=list)
    delta: str = "7"
    token: str = ""
    removed: bool = False

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: int,
        *,
        input_data: str | None = None,
    ) -> Result:
        if any(match in " ".join(argv) for match in self.overrides):
            result: Result = super().run(argv, env, timeout, input_data=input_data)
            return result
        if argv[1] in {"top", "stop", "start", "logs", "ps"}:
            self.calls.append((list(argv), dict(env), timeout))
            return Result(
                0,
                "abcdef012345"
                if argv[1] == "ps"
                else "process arguments"
                if argv[1] == "top"
                else "owner org Admin reconciled",
            )
        if input_data is not None:
            self.uploaded.append(input_data)
            data = json.loads(input_data)
            if (
                "resourceMetrics" in data
                and data["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["name"]
                == capacity.RESET
            ):
                self.reset_uploaded = True
        if (
            "--entrypoint" in argv
            and argv[argv.index("--entrypoint") + 1] == "rm"
            and "-f" in argv
        ):
            self.calls.append((list(argv), dict(env), timeout))
            self.removed = True
            return Result(0)
        result: Result = super().run(argv, env, timeout, input_data=input_data)
        return result

    def inspect(self, argv: Sequence[str]) -> Result:
        template, name = argv[-2:]
        if template == "{{.Id}} {{.State.StartedAt}}":
            return Result(0, " ".join(self.states[name]))
        if template == "{{.State.StartedAt}}":
            return Result(0, str(self.owner_time))
        if template == "{{json .Args}}":
            return Result(0, '["run","--stability.level=experimental"]')
        if template == "{{.State.Status}}:{{.State.ExitCode}}":
            return Result(0, "exited:0")
        result: Result = super().inspect(argv)
        return result

    def compose(self, argv: Sequence[str], env: Mapping[str, str]) -> Result:
        result = None
        if "up" in argv and argv[-2:] == ["up", "-d"]:
            result = self.release_up(argv)
        elif "stop" in argv:
            self.stopped.add(argv[-1])
            result = Result(0)
        elif "start" in argv:
            self.stopped.discard(argv[-1])
            result = Result(0)
        elif "restart" in argv:
            self.delta = "2"
            result = Result(0)
        elif "run" in argv and "grafana-owner" in argv:
            result = Result(0, "abcdef012345" + "6" * 52)
        elif "top" in argv:
            result = Result(0, "process arguments")
        elif "logs" in argv and argv[-1] == "--no-color":
            result = Result(0, "grafana-owner org Admin reconciled")
        elif "exec" in argv:
            result = self.notifications(argv)
        elif "logs" in argv and argv[-1] == "rustfs":
            result = Result(0, 'Starting: rustfs\n{"console_enabled":false}\n')
        if result is None:
            inherited: Result = super().compose(argv, env)
            result = inherited
        return result

    def release_up(self, argv: Sequence[str]) -> Result:
        self.owner_time += 1
        if "/output/release/compose.yaml" in argv:
            self.states.update(
                {
                    "loki": ("new-loki", "2026-10-25T05:40:05Z"),
                    "alloy": ("alloy", "2026-10-25T05:40:07Z"),
                    "grafana": ("grafana", "2026-10-25T05:40:08Z"),
                }
            )
        return Result(0, "started")

    def notifications(self, argv: Sequence[str]) -> Result | None:
        if argv[-1] == alerts.WEBHOOK_POSTS:
            return Result(
                0,
                "path=/12345/"
                + self.token
                + '\n{"content":"[FIRING:1] Disk free below the '
                'reserve","embeds":[{}]}',
            )
        if argv[-1].endswith("/messages"):
            return Result(
                0,
                json.dumps(
                    {
                        "messages": [
                            {
                                "To": [{"Address": "smoke-to@example.invalid"}],
                                "From": {"Address": "smoke-from@example.invalid"},
                                "Subject": "[FIRING:1] Disk free below the reserve",
                            }
                        ]
                    }
                ),
            )
        return None

    def inside_response(self, argv: Sequence[str]) -> Result | None:
        if argv[-1] == "http://alloy:4318/v1/metrics":
            return Result(0, self.upload)
        if argv[-1] == "http://mimir:8080/metrics":
            return Result(0, "# TYPE cortex_build_info gauge\n\n200")
        result: Result | None = super().inside_response(argv)
        return result


@dataclass
class AdminC:
    http: HttpC
    calls: list[tuple[str, bytes, int, str]] = field(default_factory=list)
    answer: tuple[int, bytes] | Exception | None = None
    protections: list[bool] = field(default_factory=list)

    def request(
        self, url: str, password: bytes, timeout: int, *, method: str
    ) -> tuple[int, bytes]:
        self.calls.append((url, password, timeout, method))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is not None:
            return self.answer
        if "alerting/reload" in url:
            self.http.deleted_rules = True
        if "dashboards/reload" in url:
            fs: Fake = self.http.engine.fs
            path = next(
                p
                for p in fs.files
                if OUT / "email-disabled/config" in p.parents
                and p.name == "agents.yaml"
            )
            enabled = yaml.safe_load(fs.read_text(path))["providers"][0][
                "disableDeletion"
            ]
            self.protections.append(enabled)
            if enabled is False and self.http.engine.removed:
                self.http.dashboard_deleted = True
        return 200, b"{}"


def instance_c() -> tuple[Fake, EngineC, HttpC, runtime.Smoke]:
    fs, _, _, run = instance_b()
    for real in sorted((ROOT / "o11y").rglob("*")):
        if real.is_file() and real.name != "AGENTS.md":
            target = SOURCE / real.relative_to(ROOT / "o11y")
            for parent in reversed(target.parents):
                fs.mkdir(parent)
            fs.write_atomic(target, real.read_bytes(), 0o600)
    engine = EngineC(fs)
    engine.token = run.sentinels.token.decode()
    http = HttpC(engine=engine)
    admin = AdminC(http)
    run.system = replace(run.system, runner=engine, receipt_http=http, admin_http=admin)

    def random(size: int) -> bytes:
        return b"\x01" * size

    run.system = replace(run.system, random_bytes=random)
    return fs, engine, http, run
