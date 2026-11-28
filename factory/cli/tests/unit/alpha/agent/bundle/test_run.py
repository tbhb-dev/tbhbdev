"""The bundled run entry point records its build, then starts the dispatcher."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

import pytest

import tbhbdev
from tbhbdev import version
from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.bundle import run

COMMIT = "a" * 40
DIGEST = "b" * 64
PROG = "tbhbdev alpha agent run"


class Dist:
    def read_text(self, name: str) -> str:
        assert name == "direct_url.json"
        return json.dumps({"vcs_info": {"commit_id": COMMIT}, "url": "file:///x"})


def installed(_name: str) -> Dist:
    return Dist()


def build_path(name: str = "example") -> Path:
    return runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs" / f"{name}.build"


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str, bool]]:
    """Replace the dispatcher; record each call and whether the record existed."""
    calls: list[tuple[list[str], str, bool]] = []

    def dispatch(argv: list[str], prog: str) -> int:
        calls.append((argv, prog, build_path().exists()))
        return 7

    monkeypatch.setattr(run.dispatcher, "main", dispatch)
    monkeypatch.setattr(metadata, "distribution", installed)
    return calls


def test_records_the_build_before_the_dispatcher(
    started: list[tuple[list[str], str, bool]],
) -> None:
    argv = ["example", "--cli-sha256", DIGEST, "--host", "h", "--steer"]

    assert run.main(argv, PROG) == 7

    assert json.loads(build_path().read_text()) == {
        "commit": COMMIT,
        "module": tbhbdev.__file__,
        "sha256": DIGEST,
    }
    assert started == [(["example", "--host", "h", "--steer"], PROG, True)]


def test_accepts_the_joined_option_form(
    started: list[tuple[list[str], str, bool]],
) -> None:
    assert run.main([f"--cli-sha256={DIGEST}", "example", "-"], PROG) == 7

    assert json.loads(build_path().read_text())["sha256"] == DIGEST
    assert started[0][0] == ["example", "-"]


def test_a_value_after_host_is_not_the_run_name(
    started: list[tuple[list[str], str, bool]],
) -> None:
    run.main(["--host", "example", "--cli-sha256", DIGEST, "other"], PROG)

    assert build_path("other").exists()
    assert not build_path().exists()
    assert started[0][0] == ["--host", "example", "other"]


def test_uses_the_stamp_when_nothing_is_installed(
    started: list[tuple[list[str], str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> Dist:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "distribution", missing)
    monkeypatch.setattr(version, "build_module", lambda: None)

    run.main(["example", "--cli-sha256", DIGEST], PROG)

    assert json.loads(build_path().read_text())["commit"] == version.UNKNOWN
    assert len(started) == 1


def test_a_missing_digest_starts_nothing(
    started: list[tuple[list[str], str, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["example"], PROG) == 1

    assert f"{PROG}: --cli-sha256 is required" in capsys.readouterr().err
    assert not build_path().exists()
    assert started == []


def test_a_digest_option_without_a_value_starts_nothing(
    started: list[tuple[list[str], str, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["example", "--cli-sha256"], PROG) == 1

    assert "--cli-sha256 is required" in capsys.readouterr().err
    assert started == []


def test_a_missing_name_is_left_to_the_dispatcher(
    started: list[tuple[list[str], str, bool]],
) -> None:
    assert run.main(["--cli-sha256", DIGEST], PROG) == 7

    assert started == [([], PROG, False)]
    assert not build_path().exists()


def test_a_bad_name_is_refused(
    started: list[tuple[list[str], str, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main(["../x", "--cli-sha256", DIGEST], PROG) == 1

    assert f"{PROG}: run name" in capsys.readouterr().err
    assert started == []
