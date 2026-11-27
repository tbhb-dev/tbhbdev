"""Exercise service health and Compose readiness fixtures."""

import json
from typing import TYPE_CHECKING

from tbhbops.o11y import smoke

from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke.safety import require

if TYPE_CHECKING:
    from tbhbops.o11y.validate import Document

    from tbhbops_cli.o11y.smoke.runtime import Smoke

READY_TIMEOUT = 180
HEALTH_TIMEOUT = 120
FIXTURE_TIMEOUT = 30
WAIT_MARGIN = 30
READY_COMPOSE_TIMEOUT = READY_TIMEOUT + WAIT_MARGIN
FIXTURE_COMPOSE_TIMEOUT = FIXTURE_TIMEOUT + WAIT_MARGIN


def loki_write_probe(run: Smoke) -> None:
    """Wait for Loki to accept a log write."""
    # poll always overwrites this before it is observed.
    rule = None  # pragma: no mutate

    def writable() -> bool:
        nonlocal rule
        timestamp = str(int(run.system.epoch()) * 1000000000)
        payload = json.dumps(
            {
                "streams": [
                    {
                        "stream": {"service_name": "o11y-smoke-probe"},
                        "values": [[timestamp, "o11y-smoke-probe"]],
                    }
                ]
            }
        )
        result = run.inside(
            "http://loki:3100/loki/api/v1/push",
            body=True,
            args=(
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-d",
                payload,
            ),
        )
        rule = smoke.loki_write_rule(result.code, result.stdout)
        return rule is None

    require(poll(run.system, READY_TIMEOUT, writable), f"5: Loki write probe: {rule}")


def wait_ready(run: Smoke, service: str, group: int) -> None:
    def ready() -> bool:
        result = run.inside(smoke.READY[service])
        return result.code == 0 and result.stdout.rstrip("\n") == "200"

    require(
        poll(run.system, READY_TIMEOUT, ready),
        f"{group}: {service} did not become ready",
    )


def group3(run: Smoke) -> None:
    for service in smoke.SERVICES:
        wait_ready(run, service, 3)
    for service in smoke.SERVICES:

        def healthy(name: str = service) -> bool:
            result = run.inspect(name, "{{.State.Health.Status}}")
            return result.code == 0 and result.stdout.strip() == "healthy"

        require(
            poll(run.system, HEALTH_TIMEOUT, healthy),
            f"3: {service} is not healthy",
        )
    run.emit(f"ok 3: {smoke.SUCCESS['3']}")
    response = run.inside("http://loki:3100/config", body=True)
    rule = smoke.wal_threshold(response.code, response.stdout)
    require(rule is None, f"3: Loki WAL threshold: {rule}")
    run.emit(smoke.MESSAGES["smoke_loki_wal_threshold_0_99"])


def group3a(run: Smoke) -> None:
    require(
        run.logged(
            ["up", "-d", "--wait", "--wait-timeout", str(READY_TIMEOUT)],
            "wait.log",
            timeout=READY_COMPOSE_TIMEOUT,
        ).code
        == 0,
        smoke.FAILURES["3a_unmodified_up_wait_failed"],
    )
    image = run.model["mimir-health-tools"]["image"]
    fixtures = {
        "readiness-unhealthy": {
            "image": image,
            "container_name": run.project + "-readiness-unhealthy",
            "restart": "unless-stopped",
            "command": ["sleep", "300"],
            "healthcheck": {
                "test": ["CMD", "false"],
                "interval": "1s",
                "timeout": "1s",
                "retries": 1,
            },
        },
    }
    fixture_check(
        run, fixtures, "readiness-unhealthy", "{{.State.Health.Status}}", "unhealthy"
    )
    fixtures = {
        "readiness-failed": {
            "image": image,
            "container_name": run.project + "-readiness-failed",
            "restart": "no",
            "command": ["sh", "-c", "exit 42"],
        },
        "readiness-consumer": {
            "image": image,
            "container_name": run.project + "-readiness-consumer",
            "restart": "no",
            "command": ["true"],
            "depends_on": {
                "readiness-failed": {"condition": "service_completed_successfully"}
            },
        },
    }
    fixture_check(
        run,
        fixtures,
        "readiness-failed",
        "{{.State.Status}}:{{.State.ExitCode}}",
        "exited:42",
    )
    run.fs.unlink(run.out / "readiness-fixture.yaml")
    run.emit(f"ok 3a: {smoke.SUCCESS['3a']}")


def fixture_check(
    run: Smoke, fixtures: dict[str, Document], name: str, template: str, expected: str
) -> None:
    run.fs.write_atomic(
        run.out / "readiness-fixture.yaml",
        json.dumps({"services": fixtures}),
        0o600,
    )
    up = run.logged(
        ["up", "-d", "--wait", "--wait-timeout", str(FIXTURE_TIMEOUT)],
        name + ".log",
        fixture=True,
        timeout=FIXTURE_COMPOSE_TIMEOUT,
    )
    require(
        up.code is not None and up.code != 0,
        smoke.FAILURES["3a_up_wait_accepted_fixture_or_timed_out"],
    )
    state = run.inspect(name, template, fixture=True)
    require(
        state.code == 0 and state.stdout.strip() == expected,
        smoke.FAILURES["3a_fixture_failed_for_an_unintended_cause"],
    )
    require(
        run.compose(["rm", "-sf", *fixtures], fixture=True).code == 0,
        smoke.FAILURES["3a_fixture_removal_failed"],
    )
