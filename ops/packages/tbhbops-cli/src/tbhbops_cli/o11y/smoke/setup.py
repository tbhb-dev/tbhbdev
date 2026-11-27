"""Prepare the smoke release and exercise host data initialization."""

import argparse
import hashlib
import io
import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbops.o11y import init, smoke

from tbhbops_cli.o11y import init as initialization
from tbhbops_cli.o11y import render
from tbhbops_cli.o11y.smoke.safety import (
    present,
    private_directory,
    require,
)

if TYPE_CHECKING:
    from tbhbops_cli.o11y.smoke.runtime import Smoke


def _private_tree(run: Smoke, path: Path) -> None:
    for name in run.fs.list_dir(path):
        child = path / name
        info = present(
            run.fs.lstat(child), smoke.FAILURES["setup_rendered_file_unavailable"]
        )
        if stat.S_ISDIR(info.st_mode):
            _private_tree(run, child)
        else:
            run.fs.chmod(child, 0o600)
    run.fs.chmod(path, 0o700)


def reclaim(run: Smoke) -> None:
    """Let in-process init inspect Linux files after service ownership checks."""
    if not run.linux:
        return
    directories = (
        ("secrets",) if run.started else tuple(d.name for d in init.DIRECTORIES)
    )
    paths = [run.destination + "/" + name for name in directories]
    paths.extend(
        run.destination + "/secrets/" + n
        for n in (*dict(run.sentinels.values), "s3.env")
    )
    existing = [
        p
        for p in paths
        if run.fs.lstat(run.data / p.removeprefix(run.destination + "/")) is not None
    ]
    if existing:
        require(
            run.utility(
                ["chown", f"{run.system.euid}:{run.system.egid}", *existing]
            ).code
            == 0,
            smoke.FAILURES["init_host_ownership_unavailable"],
        )
    if run.started:
        reclaim_guards(run)


def reclaim_guards(run: Smoke) -> None:
    # Preserve service ownership while allowing the host to lstat guards.
    # The private data root still prevents other host users traversing it.
    for name in sorted(
        {Path(s.guard).parts[0] for s in init.SECRETS if s.guard is not None}
    ):
        require(
            run.utility(["chmod", "751", run.destination + "/" + name]).code == 0,
            smoke.FAILURES["init_host_ownership_unavailable"],
        )


def initialize(run: Smoke) -> init.Plan | init.Refusal:
    plan = init.plan(
        initialization.gather(run.fs, run.host, run.system.euid),
        (run.system.euid, run.system.egid),
    )
    if isinstance(plan, init.Refusal):
        return plan
    for step in plan.steps:
        if run.linux and run.started and isinstance(step, init.EnsureDir):
            # sync_data applies these owners and modes through the root utility;
            # changing a live backend's owner here would deny it its own data.
            continue
        private = (
            replace(step, uid=run.system.euid, gid=run.system.egid)
            if isinstance(step, (init.EnsureDir, init.SetOwnerMode))
            else step
        )
        initialization.execute(
            private, run.fs, run.host, run.system.choice, io.StringIO()
        )
    return plan


def sync_data(run: Smoke) -> None:
    run.current_values = values(run)
    if not run.linux:
        if run.started:
            args = ["cp", "-a", "/host/secrets/.", run.destination + "/secrets/"]
        else:
            args = ["cp", "-a", "/host/.", run.destination + "/"]
        require(
            run.utility(args, host=True).code == 0,
            smoke.FAILURES["init_data_copy_failed"],
        )
    for name, (uid, gid, mode, _) in smoke.layout(
        run.current_values, (run.system.euid, run.system.egid)
    ).items():
        target = run.destination + "/" + name
        require(
            run.utility(["chown", f"{uid}:{gid}", target]).code == 0,
            smoke.FAILURES["init_owner_update_failed"],
        )
        require(
            run.utility(["chmod", f"{mode:o}", target]).code == 0,
            smoke.FAILURES["init_mode_update_failed"],
        )


def values(run: Smoke) -> dict[str, bytes]:
    return {
        name: run.fs.read_bytes(run.host / "secrets" / name)
        for name in dict(run.sentinels.values)
    }


def initialize_and_sync(run: Smoke) -> None:
    reclaim(run)
    require(isinstance(initialize(run), init.Plan), smoke.FAILURES["1_init_failed"])
    sync_data(run)


def seed(run: Smoke, name: str, value: bytes) -> None:
    run.fs.write_atomic(run.host / "secrets" / name, value, 0o600)


def consistent(run: Smoke) -> None:
    require(
        run.fs.read_bytes(run.host / "secrets/s3.env") == smoke.s3_env(values(run)),
        smoke.FAILURES["1_s3_env_does_not_match_the_key_files"],
    )


def fingerprint(run: Smoke) -> tuple[tuple[str, int, int, int, int, bytes], ...]:
    # s3.env is rewritten atomically by ops init, so exclude its timestamp.
    paths = run.fs.list_dir(run.host / "secrets")
    result = []
    for name in paths:
        path = run.host / "secrets" / name
        info = present(run.fs.lstat(path), smoke.FAILURES["1_fingerprint_unavailable"])
        stamp = 0 if name == "s3.env" else info.st_mtime_ns
        result.append(
            (
                name,
                info.st_uid,
                info.st_gid,
                info.st_mode,
                stamp,
                hashlib.sha256(run.fs.read_bytes(path)).digest(),
            )
        )
    for directory in init.DIRECTORIES:
        info = present(
            run.fs.lstat(run.host / directory.name),
            smoke.FAILURES["1_fingerprint_unavailable"],
        )
        result.append(
            (directory.name + "/", info.st_uid, info.st_gid, info.st_mode, 0, b"")
        )
    return tuple(result)


def check_layout(run: Smoke) -> None:
    expected = smoke.layout(run.current_values, (run.system.euid, run.system.egid))
    actual = {}
    for name, (_, _, _, size) in expected.items():
        result = run.utility(
            ["stat", "-c", "%u %g %a %s", run.destination + "/" + name]
        )
        require(result.code == 0, smoke.FAILURES["1_layout_unavailable"])
        uid, gid, mode, length = result.stdout.split()
        actual[name] = (
            int(uid),
            int(gid),
            int(mode, 8),
            None if size is None else int(length),
        )
    rule = smoke.layout_rule(actual, expected)
    require(rule is None, f"1: {rule}")


def group1(run: Smoke) -> None:
    values = dict(run.sentinels.values)
    for secret in init.SECRETS:
        seed(run, secret.name, values[secret.name])
    initialize_and_sync(run)
    check_layout(run)
    reclaim(run)
    placeholders = values(run)
    require(
        placeholders[init.WEBHOOK] == init.PLACEHOLDER.encode()
        and placeholders[init.SMTP] == b"",
        smoke.FAILURES["1_alert_placeholders_differ"],
    )
    for name in (init.WEBHOOK, init.SMTP):
        seed(run, name, values[name])
    initialize_and_sync(run)
    check_layout(run)
    reclaim(run)
    require(
        values(run) == values,
        smoke.FAILURES["1_init_replaced_a_valid_existing_secret"],
    )
    consistent(run)
    before = fingerprint(run)
    initialize_and_sync(run)
    reclaim(run)
    require(
        fingerprint(run) == before,
        smoke.FAILURES["1_a_second_init_changed_a_file"],
    )
    run.fs.unlink(run.host / "secrets/s3.env")
    seed(run, ".tmp.s3.env", smoke.EMPTY_SECRET)
    initialize_and_sync(run)
    reclaim(run)
    require(
        fingerprint(run) == before,
        smoke.FAILURES["1_interrupted_init_recovery_changed_a_file"],
    )
    seed(run, init.SECRETS[0].name, smoke.EMPTY_SECRET)
    initialize_and_sync(run)
    reclaim(run)
    consistent(run)
    require(
        values(run)[init.SECRETS[0].name] != values[init.SECRETS[0].name],
        smoke.FAILURES["1_truncated_key_was_left_in_place"],
    )
    seed(run, init.SECRETS[0].name, values[init.SECRETS[0].name])
    initialize_and_sync(run)
    reclaim(run)
    require(
        values(run) == values,
        smoke.FAILURES["1_sentinel_key_could_not_be_restored"],
    )
    consistent(run)
    sync_data(run)
    run.emit(f"ok 1: {smoke.SUCCESS['1']}")


def prepare(run: Smoke, source: Path) -> None:
    private_directory(run.fs, run.out)
    run.output_ready = True
    private_directory(run.fs, run.artifacts)
    run.emit(smoke.MESSAGES["smoke_setup"])
    render.dispatch(
        argparse.Namespace(source=source, out=run.out, config_root=run.data / "config"),
        replace(run.system, env=run.env),
    )
    _private_tree(run, run.out)
    for name in ("compose.yaml", "compose.tools.yaml"):
        release = run.out / "release"
        result = run.run(
            [
                "docker",
                "compose",
                "-p",
                run.project,
                "--env-file",
                str(release / ".env.validate"),
                "-f",
                str(release / name),
                "config",
                "-q",
            ]
        )
        require(
            result.code == 0,
            smoke.FAILURES["setup_model_does_not_load_without_secrets"],
        )
    model = run.compose(["config", "--format", "json"], tools=True, dummy=True)
    require(model.code == 0, smoke.FAILURES["setup_model_unavailable"])
    run.model = json.loads(model.stdout)["services"]
    # ops has no init Compose service; RustFS supplies the root utilities.
    image = run.model["rustfs"]["image"]
    require(
        isinstance(image, str) and bool(image),
        smoke.FAILURES["setup_utility_image_unavailable"],
    )
    run.image = image
    require(
        run.compose(["down", "-v", "--remove-orphans"], tools=True, dummy=True).code
        == 0,
        smoke.FAILURES["setup_leftover_project_cleanup_failed"],
    )
    if not run.linux:
        require(
            run.utility(["rm", "-rf", run.destination]).code == 0,
            smoke.FAILURES["setup_leftover_data_cleanup_failed"],
        )
    run.fs.mkdir(run.host)
    run.fs.mkdir(run.host / "secrets")
    run.env["O11Y_S3_ENV_FILE"] = str(run.host / "secrets/s3.env")
