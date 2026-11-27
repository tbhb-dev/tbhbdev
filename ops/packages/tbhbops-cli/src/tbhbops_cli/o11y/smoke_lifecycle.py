"""Release recreation, restarts, sentinel search and email transition stages."""

import argparse
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbops.o11y import smoke, validate
from tbhbops.o11y import smoke_access as access
from tbhbops.o11y import smoke_query as query

from tbhbops_cli.o11y import render, smoke_checks, smoke_stages
from tbhbops_cli.o11y.polling import poll
from tbhbops_cli.o11y.smoke import require, scan_path

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbops_cli.o11y.smoke import Smoke

HTTP_OK = 200
HEALTH_TIMEOUT = 180
COPY_TIMEOUT = 60
OWNER_TIMEOUT = 30
FULL_ID_LENGTH = 64
SHORT_ID_LENGTH = 12


def render_release(
    run: Smoke, source: Path, output: Path, *, email: bool = True
) -> None:
    env = dict(run.env)
    if email is False:
        env.pop("O11Y_ALERT_EMAIL_TO")
    render.dispatch(
        argparse.Namespace(source=source, out=output, config_root=run.data / "config"),
        replace(run.system, env=env),
    )
    require(
        run.utility(
            ["cp", "-a", "/src/.", run.destination + "/config/"],
            source=output / "config",
        ).code
        == 0,
        "release: config-copy",
    )
    require(
        run.utility(["chmod", "-R", "a+rX", run.destination + "/config"]).code == 0,
        "release: config-permissions",
    )
    run.release = output / "release"


def health(run: Smoke, group: int) -> None:
    for service in ("loki", "mimir"):

        def healthy(service: str = service) -> bool:
            result = run.inspect(service, "{{.State.Health.Status}}")
            return result.code == 0 and result.stdout.strip() == "healthy"

        require(poll(run.system, HEALTH_TIMEOUT, healthy), f"{group}: backend-health")


def owner(run: Smoke, group: int) -> None:
    for login, role in ((access.OWNER, "Admin"), (access.VIEWER, "Editor")):
        code, body = smoke_stages.request(run, "/api/user/orgs", login=login)
        require(
            code == HTTP_OK and access.orgs(body, role), f"{group}: stored-org-role"
        )


def owner_started(run: Smoke) -> str:
    result = run.inspect("grafana-owner", "{{.State.StartedAt}}", all_containers=True)
    require(result.code == 0 and bool(result.stdout.strip()), "8: owner-start-time")
    return result.stdout.strip()


def group8(run: Smoke, source: Path) -> None:
    before = run.container_ids()
    started = owner_started(run)
    render_release(run, source, run.out / "again")
    require(run.logged(["up", "-d"], "up.log").code == 0, "8: identical-up")
    require(owner_started(run) != started, "8: owner-rerun")
    owner(run, 8)
    require(run.container_ids() == before, "8: identical-container-states")
    health(run, 8)
    copied = run.out / "src"
    run.fs.mkdir(copied)
    for name, content in render.tree_files(run.fs, source).items():
        target = copied / name
        parent = copied
        for part in target.relative_to(copied).parts[:-1]:
            parent /= part
            run.fs.mkdir(parent)
        run.fs.write_atomic(target, content, 0o600)
    path = copied / "config/loki/config.yaml"
    run.fs.write_atomic(path, run.fs.read_bytes(path) + b"\n#\n", 0o600)
    render_release(run, copied, run.out)
    require(run.logged(["up", "-d"], "up.log").code == 0, "8: changed-up")
    require(
        smoke.recreated(before, run.container_ids()) is None,
        "8: changed-container-states",
    )
    for service in ("loki", "alloy", "grafana"):
        run.wait_ready(service, 8)
    health(run, 8)
    run.emit(
        "ok 8: identical releases preserve containers; changed Loki "
        "recreates only Loki and restarts its dependents in place"
    )


def group9(run: Smoke) -> None:
    require(run.compose(["restart"]).code == 0, "9: compose-restart")
    for service in smoke.SERVICES:
        run.wait_ready(service, 9)
    owner(run, 9)
    health(run, 9)

    def copied() -> bool:
        result = run.inspect(
            "mimir-health-tools",
            "{{.State.Status}}:{{.State.ExitCode}}",
            all_containers=True,
        )
        return result.code == 0 and result.stdout.strip() == "exited:0"

    require(poll(run.system, COPY_TIMEOUT, copied), "9: restarted-copy-exit")
    ids = smoke.SERVICES
    containers = []
    for service in reversed(ids):
        result = run.compose(["ps", "-q", service])
        require(
            result.code == 0 and bool(result.stdout.strip()), "9: service-container-id"
        )
        containers.append(result.stdout.strip())
    require(run.run(["docker", "stop", *containers]).code == 0, "9: direct-stop")
    require(run.run(["docker", "start", *containers]).code == 0, "9: direct-start")
    for service in smoke.SERVICES:
        run.wait_ready(service, 9)
    owner(run, 9)
    health(run, 9)
    smoke_checks.helper_receipt(run, "9")
    run.emit(
        "ok 9: Compose restart and direct container starts recover "
        "readiness, health, owner role and receipts"
    )


def owner_identity(run: Smoke, printed: str) -> tuple[str, str]:
    result = run.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + run.project,
            "--filter",
            "label=com.docker.compose.service=grafana-owner",
        ]
    )
    ids = result.stdout.split()
    if result.code != 0:
        return printed, "container-query"
    if not ids:
        return printed, "zero-containers"
    if len(ids) != 1:
        return printed, "several-containers"
    if printed == ids[0] or (
        len(printed) == FULL_ID_LENGTH and printed[:SHORT_ID_LENGTH] == ids[0]
    ):
        return ids[0], ""
    return printed, "identity-mismatch"


def group10(run: Smoke) -> None:
    repeated = run.compose(
        [
            "run",
            "--rm",
            "-d",
            "-T",
            "--entrypoint",
            "/bin/sh",
            "grafana-owner",
            "-c",
            "while :; do /bin/sh /etc/o11y-grafana/owner.sh || exit; done",
        ]
    )
    require(
        repeated.code == 0 and bool(repeated.stdout.strip()), "10: repeated-owner-start"
    )
    identity = repeated.stdout.strip()
    processes = []
    try:
        identity, reason = owner_identity(run, identity)

        def running() -> bool:
            nonlocal identity, reason
            identity, reason = owner_identity(run, identity)
            return not reason

        ready = not reason or poll(run.system, OWNER_TIMEOUT, running)
        require(ready, "10: repeated-owner-running: " + reason)
        top = run.run(["docker", "top", identity])
        require(top.code == 0, "10: owner-process-arguments")
        processes.append(top.stdout + top.stderr)
        # poll always overwrites this before it is observed.
        output = ""  # pragma: no mutate

        def reconciled() -> bool:
            nonlocal output
            log = run.run(["docker", "logs", identity])
            output = log.stdout + log.stderr
            return log.code == 0 and "org Admin reconciled" in output

        require(
            poll(run.system, OWNER_TIMEOUT, reconciled), "10: repeated-owner-output"
        )
        run.fs.write_atomic(
            run.out / "owner-repeated.log",
            output.encode(errors="surrogateescape"),
            0o600,
        )
    finally:
        failure = sys.exception()
        stopped = run.run(["docker", "stop", identity]).code == 0
        require(stopped or failure is not None, "10: repeated-owner-stop")
    for service in smoke.SERVICES:
        top = run.compose(["top", service])
        require(top.code == 0, "10: process-arguments")
        processes.append(top.stdout + top.stderr)
    run.fs.write_atomic(
        run.out / "processes.log",
        "\n".join(processes).encode(errors="surrogateescape"),
        0o600,
    )
    logs = run.logged(["logs", "--no-color"], "containers.log")
    require(
        logs.code == 0 and "org Admin reconciled" in logs.stdout + logs.stderr,
        "10: owner-log-searched",
    )
    printing = replace(run, env=validate.docker_environment(run.env))
    model = printing.compose(["config", "--format", "json"], dummy=True)
    require(model.code == 0, "10: printed-model")
    run.fs.write_atomic(
        run.out / "model.log",
        (model.stdout + model.stderr).encode(errors="surrogateescape"),
        0o600,
    )
    run.finalize_transcript()
    for name in (
        "release",
        "again",
        "src",
        "config",
        "containers.log",
        "owner-repeated.log",
        "processes.log",
        "model.log",
        "transcript.log",
    ):
        scan_path(run.fs, run.out / name, run.sentinels.scan_values())
    run.emit(
        "ok 10: rendered output, container logs, current process "
        "arguments, model and transcript contain no sentinel"
    )


def group13(run: Smoke, source: Path) -> None:
    started = run.system.monotonic()
    render_release(run, source, run.out / "email-disabled", email=False)
    require(
        run.logged(["up", "-d", "--no-deps", "grafana"], "email-disabled.log").code
        == 0,
        "13: email-disabled-up",
    )
    run.wait_ready("grafana", 13)
    require(
        query.email_disabled(
            smoke_checks.get(run, "/api/v1/provisioning/contact-points", "13")
        ),
        "13: email-receiver-deleted",
    )
    require(
        run.logged(["logs", "--no-color", "grafana"], "email-disabled-grafana.log").code
        == 0,
        "13: grafana-log-read",
    )
    for name in ("email-disabled", "email-disabled.log", "email-disabled-grafana.log"):
        scan_path(run.fs, run.out / name, run.sentinels.scan_values())
    run.emit(
        f"ok 13: disabling email deletes its stored receiver and "
        f"preserves Discord ({int(run.system.monotonic() - started)} seconds)"
    )
