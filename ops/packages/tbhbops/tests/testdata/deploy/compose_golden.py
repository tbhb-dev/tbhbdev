# ruff: noqa: INP001, S101, S603
"""Run only the pinned Compose parser, without a daemon or secret files."""

import json
import re
import shutil
import subprocess  # nosec B404: pinned parser subprocess in a test
from pathlib import Path

import pytest
import yaml

from tbhbops.o11y import render

FIXTURES = Path(__file__).parent
ROOT = FIXTURES.parents[4]


@pytest.mark.parametrize("name", ["minimal", "o11y"])
def test_normalized_model(name: str, tmp_path: Path) -> None:
    binary = shutil.which("deploy-compose-v5.7.1")
    assert binary is not None  # nosec B101: pytest assertion
    version = subprocess.run(  # nosec B603: pinned parser, argv only
        [binary, "version", "--short"], check=True, capture_output=True, env={}
    )
    assert version.stdout.strip() == b"5.7.1"  # nosec B101
    source = (
        ROOT / "o11y" / "compose.yaml" if name == "o11y" else FIXTURES / "minimal.yaml"
    )
    expected = json.loads((FIXTURES / f"{name}.json").read_text())
    if name == "o11y":
        # Exercise the receiver's qualified deployment input within this fixture.
        source_text = re.sub(
            r"(?m)^(\s+image: )([^\s]+)",
            lambda match: (
                match[1]
                + ("docker.io/" if "/" in match[2] else "docker.io/library/")
                + match[2]
            ),
            render.substitute(source.read_text(), render.Settings((), 50, email=False)),
        )
        source = tmp_path / "compose.yaml"
        # The deployment fixture adds the limits required by receiver policy.
        qualified = yaml.safe_load(source_text)
        for service in qualified["services"].values():
            service.setdefault("mem_limit", "128m")
            service["pids_limit"] = 256
        source.write_text(yaml.safe_dump(qualified))
        expected = expected["model"]
    base = [
        binary,
        "-p",
        "o11y",
        "--project-directory",
        "/srv/o11y/releases/" + "a" * 40,
    ]
    result = subprocess.run(  # nosec B603: pinned parser, argv only
        [
            *base,
            "-f",
            str(source),
            "--env-file",
            str(FIXTURES / "compose.env"),
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        env={},
        check=True,
        capture_output=True,
    )
    assert json.loads(result.stdout) == expected  # nosec B101
    stored = tmp_path / "model.json"
    stored.write_bytes(result.stdout)
    fixed = subprocess.run(  # nosec B603: pinned parser, argv only
        [
            *base,
            "-f",
            str(stored),
            "--env-file",
            "/dev/null",
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        env={},
        check=True,
        capture_output=True,
    )
    assert json.loads(fixed.stdout) == expected  # nosec B101
