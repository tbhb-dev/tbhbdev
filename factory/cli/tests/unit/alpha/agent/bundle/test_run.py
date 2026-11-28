"""The bundled run entry point records its build under the dispatcher's lock."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import tbhbdev
from tbhbdev import version
from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.bundle import run

if TYPE_CHECKING:
    import argparse

COMMIT = "a" * 40
DIGEST = "b" * 64
OTHER_DIGEST = "c" * 64
PROG = "tbhbdev alpha agent run"


class Dist:
    def read_text(self, name: str) -> str:
        assert name == "direct_url.json"
        return json.dumps({"vcs_info": {"commit_id": COMMIT}, "url": "file:///x"})


class StubRun:
    def __init__(self, files: runfiles.RunFiles) -> None:
        self.files = files


def installed(_name: str) -> Dist:
    return Dist()


def build_path(name: str = "example") -> Path:
    return runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs" / f"{name}.build"


@pytest.fixture
def started(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[argparse.Namespace, bool]]:
    """Run the real dispatcher checks, stopping before harness execution."""
    calls: list[tuple[argparse.Namespace, bool]] = []
    loaded: list[argparse.Namespace] = []

    def load(args: argparse.Namespace, _tmpdir: Path) -> tuple[StubRun, str]:
        loaded.append(args)
        return StubRun(run.dispatcher.intake.files_for(args.name)), ""

    def execute(active: StubRun, _text: str, *, preflight_only: bool = False) -> int:
        del preflight_only
        calls.append((loaded[-1], active.files.path("build").exists()))
        return 7

    monkeypatch.setattr(run.dispatcher.intake, "load", load)
    monkeypatch.setattr(run.dispatcher.build_info, "line", lambda: "tbhbdev")
    monkeypatch.setattr(run.dispatcher.execution, "execute", execute)
    monkeypatch.setattr(metadata, "distribution", installed)
    return calls


def test_records_the_build_before_execution(
    started: list[tuple[argparse.Namespace, bool]],
) -> None:
    argv = ["example", "--cli-sha256", DIGEST, "--host", "h", "--steer"]

    assert run.main(argv, PROG) == 7

    assert json.loads(build_path().read_text()) == {
        "commit": COMMIT,
        "module": tbhbdev.__file__,
        "sha256": DIGEST,
    }
    assert len(started) == 1
    args, existed = started[0]
    assert (args.name, args.host, args.steer, existed) == ("example", "h", True, True)


def test_refused_rerun_preserves_the_build_record(
    started: list[tuple[argparse.Namespace, bool]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run.main(["example", "--cli-sha256", DIGEST, "--host", "h"], PROG) == 7
    original = build_path().read_bytes()

    assert run.main(["example", "--cli-sha256", OTHER_DIGEST, "--host", "h"], PROG) == 1

    assert build_path().read_bytes() == original
    assert "already has an owner" in capsys.readouterr().err
    assert len(started) == 1


@pytest.mark.parametrize("marker", ["rc", "pgid"])
def test_refused_owned_rerun_preserves_the_build_record(
    started: list[tuple[argparse.Namespace, bool]],
    capsys: pytest.CaptureFixture[str],
    marker: str,
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs", "example"
    )
    files.write("owner", "driver\n")
    files.write("build", json.dumps({"sha256": DIGEST}) + "\n")
    files.write(marker, "7\n")
    original = build_path().read_bytes()

    assert run.main(["example", "--owned", "--cli-sha256", OTHER_DIGEST], PROG) == 1

    assert build_path().read_bytes() == original
    assert "already started" in capsys.readouterr().err
    assert started == []


def test_accepts_the_joined_option_form(
    started: list[tuple[argparse.Namespace, bool]],
) -> None:
    assert run.main([f"--cli-sha256={DIGEST}", "example", "-"], PROG) == 7

    assert json.loads(build_path().read_text())["sha256"] == DIGEST
    assert (started[0][0].name, started[0][0].request) == ("example", "-")


def test_a_value_after_host_is_not_the_run_name(
    started: list[tuple[argparse.Namespace, bool]],
) -> None:
    assert (
        run.main(
            ["--steer", "--host", "example", "--cli-sha256", DIGEST, "other"], PROG
        )
        == 7
    )

    assert build_path("other").exists()
    assert not build_path().exists()
    args = started[0][0]
    assert (args.name, args.host, args.steer) == ("other", "example", True)


def test_uses_the_stamp_when_nothing_is_installed(
    started: list[tuple[argparse.Namespace, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> Dist:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "distribution", missing)
    monkeypatch.setattr(version, "build_module", lambda: None)

    assert run.main(["example", "--cli-sha256", DIGEST], PROG) == 7

    assert json.loads(build_path().read_text())["commit"] == version.UNKNOWN
    assert len(started) == 1


def test_a_missing_digest_starts_nothing(
    started: list[tuple[argparse.Namespace, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["example"], PROG) == 1

    assert f"{PROG}: --cli-sha256 is required" in capsys.readouterr().err
    assert not build_path().exists()
    assert started == []


def test_a_digest_option_without_a_value_starts_nothing(
    started: list[tuple[argparse.Namespace, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["example", "--cli-sha256"], PROG) == 1

    assert "--cli-sha256 is required" in capsys.readouterr().err
    assert started == []


def test_a_missing_name_is_left_to_the_dispatcher(
    started: list[tuple[argparse.Namespace, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["--cli-sha256", DIGEST], PROG) == 1

    assert f"{PROG}: run name is required" in capsys.readouterr().err
    assert started == []
    assert not build_path().exists()


def test_a_bad_name_is_refused(
    started: list[tuple[argparse.Namespace, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["../x", "--cli-sha256", DIGEST], PROG) == 1

    assert f"{PROG}: run name" in capsys.readouterr().err
    assert started == []
