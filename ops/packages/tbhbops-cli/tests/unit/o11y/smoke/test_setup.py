"""Smoke release setup and host initialization."""

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from tbhbops.o11y import init, smoke
from tbhbops_cli.o11y import init as initialization
from tbhbops_cli.o11y.runner import Result
from tbhbops_cli.o11y.smoke import (
    bootstrap,
    runtime,
    safety,
)
from tbhbops_cli.o11y.smoke import setup as setup_stage
from unit.o11y.smoke.runtime_fixture import (
    HOST,
    OUT,
    make_system,
    model,
    prepared,
    run,
)
lazy from tbhbops_cli.o11y.filesystem import Filesystem


def test_present_missing_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    fs, engine, instance = prepared()
    engine.fail_match = "s3probe buckets"
    engine.replacement = Result(0, "invalid")
    with pytest.raises(safety.SmokeError, match="bucket inventory differs"):
        bootstrap.inventory(instance)
    fs.modes[HOST / "secrets/missing"] = stat.S_IFREG
    original = fs.lstat

    def missing(path: Path) -> object:
        return None if path.name in ("missing", "compose.yaml") else original(path)

    monkeypatch.setattr(fs, "lstat", missing)
    with pytest.raises(safety.SmokeError, match="fingerprint unavailable"):
        setup_stage.fingerprint(instance)
    with pytest.raises(safety.SmokeError, match="rendered file unavailable"):
        setup_stage._private_tree(
            instance, OUT
        )  # a disappearing rendered file must fail


def test_transcript_private_write_and_init_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, instance = prepared()
    instance.finalize_transcript()
    assert ("write", OUT / "transcript.log", 0o600) in fs.calls

    def refusal(_facts: init.Facts, _owner: tuple[int, int]) -> init.Refusal:
        return init.Refusal("synthetic refusal")

    monkeypatch.setattr(init, "plan", refusal)
    with pytest.raises(safety.SmokeError, match=r"^1: init failed$"):
        setup_stage.initialize_and_sync(instance)


def test_private_render_and_host_init_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, instance = prepared()
    for path in fs.modes:
        if path == OUT or OUT in path.parents:
            assert fs.modes[path] & 0o777 == (
                0o700 if stat.S_ISDIR(fs.modes[path]) else 0o600
            )
    env_text = fs.read_text(OUT / "release/.env")
    assert "O11Y_CONFIG_GRAFANA=/temporary/config/grafana-" in env_text
    assert "O11Y_CONFIG_LOKI=/temporary/config/loki-" in env_text
    assert isinstance(setup_stage.initialize(instance), init.Plan)
    for spec in init.DIRECTORIES:
        assert fs.owners[HOST / spec.name] == (123, 456)
    for spec in init.SECRETS:
        assert fs.owners[HOST / "secrets" / spec.name] == (123, 456)
    setup_stage.seed(instance, "new", b"X")
    assert fs.modes[HOST / "secrets/new"] & 0o777 == 0o600
    # init's access-sensitive gather must get the actual effective uid.
    original = initialization.gather

    def gather(filesystem: Filesystem, path: Path, euid: int) -> init.Facts:
        assert euid == 123
        return original(filesystem, path, euid)

    monkeypatch.setattr(initialization, "gather", gather)
    assert isinstance(setup_stage.initialize(instance), init.Plan)


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize("image", ["", 17, None])
def test_invalid_model_image(image: object) -> None:
    _, engine, system = make_system()
    document = model()
    document["services"]["rustfs"]["image"] = image
    engine.fail_match = "config --format json"
    engine.replacement = Result(0, json.dumps(document))
    code, output = run(system)
    assert code == 1
    assert "FAIL setup: utility image unavailable\n" in output


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("match", "rule"),
    [
        ("rm rustfs:pinned -rf /d/smoke", "setup: leftover data cleanup failed"),
        ("cp rustfs:pinned -a /host/.", "init: data copy failed"),
        (
            "cp rustfs:pinned -a /d/smoke-01010101/rustfs/.rustfs.sys",
            "2: RustFS guard copy failed",
        ),
        ("chown rustfs:pinned -R", "2: RustFS guard ownership failed"),
    ],
)
def test_macos_copy_failure_rules(match: str, rule: str) -> None:
    fs, engine, system = make_system()
    engine.identity = "docker-desktop|Docker Desktop"
    engine.fail_match = match
    code, output = run(replace(system, platform="darwin"))
    assert code == 1
    assert "FAIL " + rule + "\n" in output
    assert fs.lstat(OUT) is None


@pytest.mark.parametrize(
    ("number", "secret", "replacement", "rule"),
    [
        (2, init.WEBHOOK, b"bad", "1: alert placeholders differ"),
        (2, init.SMTP, b"bad", "1: alert placeholders differ"),
        (4, init.SECRETS[0].name, b"bad", "1: init replaced a valid existing secret"),
        (10, init.SECRETS[0].name, b"sentinel", "1: truncated key was left in place"),
        (12, init.SECRETS[0].name, b"bad", "1: sentinel key could not be restored"),
    ],
)
def test_group1_value_checks(
    number: int,
    secret: str,
    replacement: bytes,
    rule: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, instance = prepared()
    original = setup_stage.values
    calls = 0

    def values(run: runtime.Smoke) -> dict[str, bytes]:
        nonlocal calls
        calls += 1
        result = original(run)
        if calls == number:
            result[secret] = (
                dict(instance.sentinels.values)[secret]
                if replacement == b"sentinel"
                else replacement
            )
        return result

    monkeypatch.setattr(setup_stage, "values", values)
    with pytest.raises(safety.SmokeError) as failure:
        setup_stage.group1(instance)
    assert str(failure.value) == rule


@pytest.mark.parametrize(
    ("number", "rule"),
    [
        (2, "1: a second init changed a file"),
        (3, "1: interrupted init recovery changed a file"),
        (5, "2: refused init changed a file"),
    ],
)
def test_fingerprint_stage_failures(
    number: int, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, instance = prepared()
    original = setup_stage.fingerprint
    calls = 0

    def fingerprint(
        run: runtime.Smoke,
    ) -> tuple[tuple[str, int, int, int, int, bytes], ...]:
        nonlocal calls
        calls += 1
        value = original(run)
        return (*value, ("changed", 0, 0, 0, 0, b"")) if calls == number else value

    monkeypatch.setattr(setup_stage, "fingerprint", fingerprint)
    with pytest.raises(
        safety.SmokeError
    ) as failure:  # failure may occur in either ordered stage
        setup_stage.group1(instance)
        bootstrap.group2(instance)
    assert str(failure.value) == rule


def test_consistency_and_layout_failure_rules() -> None:
    fs, engine, instance = prepared()
    setup_stage.group1(instance)
    setup_stage.reclaim(instance)
    fs.write_atomic(HOST / "secrets/s3.env", b"wrong", 0o600)
    with pytest.raises(safety.SmokeError) as failure:
        setup_stage.consistent(instance)
    assert str(failure.value) == "1: s3.env does not match the key files"
    for replacement, rule in (
        (Result(17), "1: layout unavailable"),
        (Result(0, "0 0 777 0"), "1: owners, modes or sizes differ from the layout"),
    ):
        engine.fail_match = "stat rustfs:pinned -c"
        engine.replacement = replacement
        with pytest.raises(safety.SmokeError) as failure:
            setup_stage.check_layout(instance)
        assert str(failure.value) == rule


def test_fingerprint_tracks_identity_metadata_and_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, _, instance = prepared()
    assert isinstance(setup_stage.initialize(instance), init.Plan)
    original = fs.lstat

    def metadata(path: Path) -> os.stat_result | None:
        value = original(path)
        if value is None:
            return None
        fields = list(value)
        fields[4:6] = [123, 456]
        return os.stat_result(fields, {"st_mtime_ns": 123456})

    monkeypatch.setattr(fs, "lstat", metadata)
    before = setup_stage.fingerprint(instance)
    rows = {row[0]: row[1:] for row in before}
    assert set(rows) == {
        *(spec.name + "/" for spec in init.DIRECTORIES),
        *(spec.name for spec in init.SECRETS),
        init.WEBHOOK,
        init.SMTP,
        "s3.env",
    }
    for name, row in rows.items():
        uid, gid, mode, timestamp, digest = row
        assert (uid, gid) == (123, 456)
        expected_mode = (
            fs.modes[HOST / name]
            if name.endswith("/")
            else fs.modes[HOST / "secrets" / name]
        )
        assert mode == expected_mode
        if name.endswith("/"):
            assert (timestamp, digest) == (0, b"")
        else:
            assert timestamp == (0 if name == "s3.env" else 123456)
            assert len(digest) == 32
    fs.files[HOST / "secrets/rustfs.secret_key"] = "changed"
    assert setup_stage.fingerprint(instance) != before
    del fs.modes[HOST / "loki"]
    with pytest.raises(safety.SmokeError) as failure:
        setup_stage.fingerprint(instance)
    assert str(failure.value) == "1: fingerprint unavailable"


def test_live_linux_init_keeps_service_directory_owners() -> None:
    fs, engine, instance = prepared()
    setup_stage.group1(instance)
    instance.started = True
    fs.mkdir(HOST / "rustfs/.rustfs.sys")
    setup_stage.reclaim(instance)
    for name in ("rustfs", "grafana"):
        expected = next(spec for spec in init.DIRECTORIES if spec.name == name)
        assert fs.owners[HOST / name] == (expected.uid, expected.gid)
        assert fs.modes[HOST / name] & 0o777 == 0o751
    fs.unlink(HOST / "secrets/s3.env")
    assert isinstance(setup_stage.initialize(instance), init.Plan)
    assert fs.read_bytes(HOST / "secrets/s3.env") == smoke.s3_env(
        setup_stage.values(instance)
    )
    assert fs.owners[HOST / "rustfs"] == (10001, 10001)
    assert fs.owners[HOST / "grafana"] == (472, 472)
    setup_stage.sync_data(instance)
    for name in ("rustfs", "grafana"):
        assert fs.modes[HOST / name] & 0o777 == 0o750
    engine.fail_match = "chmod rustfs:pinned 751"
    engine.replacement = Result(17)
    with pytest.raises(safety.SmokeError) as failure:
        setup_stage.reclaim(instance)
    assert str(failure.value) == "init: host ownership unavailable"


@pytest.mark.usefixtures("early_stages")
@pytest.mark.parametrize(
    ("match", "rule"),
    [
        ("config -q", "setup: model does not load without secrets"),
        ("config --format json", "setup: model unavailable"),
        ("down -v --remove-orphans", "setup: leftover project cleanup failed"),
        ("--entrypoint chown", "init: host ownership unavailable"),
        ("--entrypoint chmod", "init: mode update failed"),
    ],
)
def test_stage_command_failure_rules(match: str, rule: str) -> None:
    fs, engine, system = make_system()
    engine.fail_match = match
    code, output = run(system)
    assert code == 1
    assert "FAIL " + rule + "\n" in output
    assert "untrusted" not in output
    assert fs.lstat(OUT) is None
    assert fs.lstat(HOST) is None
