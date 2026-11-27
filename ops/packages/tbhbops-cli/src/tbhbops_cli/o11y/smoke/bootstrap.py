"""Start the stack and verify object storage initialization."""

from typing import TYPE_CHECKING

from tbhbops.o11y import init, rustfs_probe, smoke

from tbhbops_cli.o11y.smoke import setup
from tbhbops_cli.o11y.smoke.safety import present, require

if TYPE_CHECKING:
    from tbhbops_cli.o11y.smoke.runtime import Smoke

PULL_TIMEOUT = 600


def inventory(run: Smoke) -> tuple[str, ...]:
    result = run.probe(["buckets"])
    require(result.code == 0, smoke.FAILURES["2_bucket_inventory_unavailable"])
    parsed = smoke.bucket_inventory(result.stdout)
    return present(parsed, smoke.FAILURES["2_bucket_inventory_differs"])


def bootstrap(run: Smoke, created: tuple[str, ...], *, first: bool = False) -> None:
    result = run.compose(
        ["logs", "--no-log-prefix", "s3-buckets"]
        if first
        else ["run", "--rm", "-T", "s3-buckets"]
    )
    require(result.code == 0, smoke.FAILURES["2_bucket_bootstrap_failed"])
    rule = smoke.bucket_log(result.stdout + result.stderr, created)
    require(rule is None, f"2: {rule}")


def group2(run: Smoke) -> None:
    # Runtime configs get their normal readable modes in the data tree;
    # rendered host output remains private throughout the run.
    require(
        run.utility(
            ["cp", "-a", "/src/.", run.destination + "/config/"],
            source=run.out / "config",
        ).code
        == 0,
        smoke.FAILURES["2_config_copy_failed"],
    )
    require(
        run.utility(["chmod", "-R", "a+rX", run.destination + "/config"]).code == 0,
        smoke.FAILURES["2_config_permissions_failed"],
    )
    run.started = True
    require(
        run.logged(["up", "-d"], "up.log", timeout=PULL_TIMEOUT).code == 0,
        smoke.FAILURES["2_up_failed"],
    )
    run.record_loki_disk("started")
    require(
        run.logged(
            ["up", "-d", "alert-stub", "alert-mail"],
            "stubs.log",
            tools=True,
            timeout=PULL_TIMEOUT,
        ).code
        == 0,
        smoke.FAILURES["2_alert_stubs_failed"],
    )
    state = run.inspect(
        "s3-buckets", "{{.State.Status}} {{.State.ExitCode}}", all_containers=True
    )
    require(
        state.code == 0 and state.stdout.strip() == "exited 0",
        smoke.FAILURES["2_s3_buckets_did_not_exit_0"],
    )
    bootstrap(run, smoke.BUCKETS, first=True)
    before = inventory(run)
    bootstrap(run, ())
    require(
        inventory(run) == before,
        smoke.FAILURES["2_a_second_run_recreated_a_bucket"],
    )
    require(
        run.probe(["delete-bucket", "mimir-ruler"]).code == 0,
        smoke.FAILURES["2_partial_bootstrap_fixture_failed"],
    )
    bootstrap(run, ("mimir-ruler",))
    rule = smoke.kept_buckets(before, inventory(run))
    require(rule is None, f"2: {rule}")
    rule = smoke.dependencies(run.model)
    require(rule is None, f"2: {rule}")
    log = run.compose(["logs", "--no-log-prefix", "rustfs"])
    require(log.code == 0, smoke.FAILURES["2_rustfs_start_log_unavailable"])
    rule = rustfs_probe.start_log(log.stdout + log.stderr)
    require(rule is None, f"2: rustfs {rule}")
    require(
        run.probe(["default-credentials"]).code == 0,
        smoke.FAILURES["2_default_login_was_not_refused"],
    )
    setup.reclaim(run)
    if not run.linux:
        # Compose reads s3.env on the host (compose.tools.yaml:24), while
        # render's default points into the VM (render.py:197).
        require(
            run.utility(
                [
                    "cp",
                    "-a",
                    run.destination + "/rustfs/.rustfs.sys",
                    "/host/rustfs/",
                ],
                host=True,
            ).code
            == 0,
            smoke.FAILURES["2_rustfs_guard_copy_failed"],
        )
        require(
            run.utility(
                [
                    "chown",
                    "-R",
                    f"{run.system.euid}:{run.system.egid}",
                    "/host/rustfs/.rustfs.sys",
                ],
                host=True,
            ).code
            == 0,
            smoke.FAILURES["2_rustfs_guard_ownership_failed"],
        )
    setup.seed(run, init.SECRETS[1].name, smoke.EMPTY_SECRET)
    before_files = setup.fingerprint(run)
    refused = setup.initialize(run)
    require(
        isinstance(refused, init.Refusal)
        and refused.message
        == (
            f"{init.SECRETS[1].name} is missing or damaged and "
            f"{init.SECRETS[1].guard} already exists; "
            "restore the file, nothing was changed"
        ),
        smoke.FAILURES["2_init_did_not_refuse_damaged_key_in_use"],
    )
    require(
        setup.fingerprint(run) == before_files,
        smoke.FAILURES["2_refused_init_changed_a_file"],
    )
    setup.seed(
        run, init.SECRETS[1].name, dict(run.sentinels.values)[init.SECRETS[1].name]
    )
    setup.initialize_and_sync(run)
    setup.reclaim(run)
    require(
        setup.values(run) == dict(run.sentinels.values),
        smoke.FAILURES["2_sentinel_key_could_not_be_restored"],
    )
    setup.sync_data(run)
    run.emit(f"ok 2: {smoke.SUCCESS['2']}")
