# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import os
import subprocess
import sys

import pytest

from lint.private.review_policy import check_review_policy as checker


@pytest.mark.parametrize("status", [0, 1, 2])
def test_verdict(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(
        command: list[str], *, env: dict[str, str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, env))
        return subprocess.CompletedProcess(command, status)

    monkeypatch.setenv("PYTHONPATH", "boot")
    monkeypatch.setattr(subprocess, "run", run)

    assert checker.main(["src", "tree"]) == status
    assert calls[0][0] == [
        sys.executable,
        "-m",
        "tbhbdev",
        "alpha",
        "review",
        "policy",
        "check",
        "tree",
    ]
    assert calls[0][1]["PYTHONPATH"].split(os.pathsep) == ["src", "boot"]


def test_python_path_without_inherited_one(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def run(
        command: list[str], *, env: dict[str, str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(env["PYTHONPATH"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(subprocess, "run", run)

    assert checker.main(["src", "tree"]) == 0
    assert seen == ["src"]
