# cspell:ignore EXAMPLEADMINACCESS EXAMPLEADMINSECRET EXAMPLEACCESSKEY
# cspell:ignore EXAMPLESECRETKEY EXAMPLEOTHER EXAMPLEOTHERSECRET EXAMPLESECRET
import io
import json
import stat
from dataclasses import replace
from typing import TYPE_CHECKING
lazy from pathlib import Path

import pytest
from tbhbops_cli import cli

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV = {
    "ADMIN_ACCESS": "EXAMPLEADMINACCESS0001",
    "ADMIN_SECRET": "EXAMPLEADMINSECRET0001",
    "CLIENT_ACCESS": "EXAMPLEACCESSKEY0001",
    "CLIENT_SECRET": "EXAMPLESECRETKEY0001",
}


def invoke(
    path: Path, *extra: str, env: Mapping[str, str] = ENV
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(
        ["s3", "render-identities", "--out", str(path), *extra],
        replace(cli.real_system(), env=env),
        out,
        err,
    )
    return code, out.getvalue(), err.getvalue()


def test_environment_and_replacement(tmp_path: Path) -> None:
    target = tmp_path / "identities.json"
    target.write_text("old")
    target.chmod(0o644)
    assert invoke(
        target,
        "--admin",
        "env:ADMIN_ACCESS",
        "env:ADMIN_SECRET",
        "--consumer",
        "o11y",
        "loki,mimir-blocks,mimir-ruler,tempo",
        "env:CLIENT_ACCESS",
        "env:CLIENT_SECRET",
        "--consumer",
        "other",
        "other",
        "env:OTHER_ACCESS",
        "env:OTHER_SECRET",
        env={
            **ENV,
            "OTHER_ACCESS": "EXAMPLEOTHER",
            "OTHER_SECRET": "EXAMPLEOTHERSECRET",
        },
    ) == (0, "", "")
    doc = json.loads(target.read_text())
    assert doc["identities"][0] == {
        "name": "admin",
        "credentials": [
            {"accessKey": ENV["ADMIN_ACCESS"], "secretKey": ENV["ADMIN_SECRET"]}
        ],
        "actions": ["Admin"],
    }
    client = doc["identities"][1]
    assert client["name"] == "o11y"
    assert client["credentials"] == [
        {"accessKey": ENV["CLIENT_ACCESS"], "secretKey": ENV["CLIENT_SECRET"]}
    ]
    assert client["actions"] == [
        f"{action}:{bucket}"
        for bucket in ("loki", "mimir-blocks", "mimir-ruler", "tempo")
        for action in ("Read", "Write", "List")
    ]
    assert doc["identities"][2]["name"] == "other"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text().endswith("\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["identities.json"]


def test_files_and_admin_only(tmp_path: Path) -> None:
    access, secret = tmp_path / "access:with-colon", tmp_path / "secret"
    access.write_bytes(b"EXAMPLEACCESSKEY0001X\r\n")
    secret.write_bytes(b" EXAMPLESECRETKEY0001 \r\n\r\n")
    target = tmp_path / "identities.json"
    assert invoke(target, "--admin", f"file:{access}", f"file:{secret}") == (0, "", "")
    assert json.loads(target.read_text()) == {
        "identities": [
            {
                "name": "admin",
                "credentials": [
                    {
                        "accessKey": "EXAMPLEACCESSKEY0001X",
                        "secretKey": " EXAMPLESECRETKEY0001 ",
                    }
                ],
                "actions": ["Admin"],
            }
        ]
    }
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "ref", ["env:", "file:", "", "literal:EXAMPLESECRET", "EXAMPLESECRET"]
)
def test_invalid_references(tmp_path: Path, ref: str) -> None:
    target = tmp_path / "out"
    code, out, err = invoke(target, "--admin", ref, "env:ADMIN_SECRET")
    assert code == 1
    assert out == ""
    reason = (
        "require a nonempty environment name or file path"
        if ref in ("env:", "file:", "", "EXAMPLESECRET")
        else "must use env:VARIABLE or file:PATH"
    )
    assert err == f"tbhbops s3 render-identities: key references {reason}\n"
    assert "EXAMPLE" not in err
    assert not target.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["s3"],
        ["s3", "render-identities"],
        ["s3", "render-identities", "--out", "unused"],
        ["s3", "render-identities", "--admin", "env:A", "env:B"],
    ],
)
def test_required_arguments(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(args)
    assert exc.value.code == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["s3"],
            (
                "usage: tbhbops s3 [-h] {render-identities} ... "
                "positional arguments: {render-identities} render-identities "
                "write private SeaweedFS identities; keys never belong in argv "
                "options: -h, --help show this help message and exit"
            ),
        ),
        (
            ["s3", "render-identities"],
            (
                "usage: tbhbops s3 render-identities [-h] --out OUT "
                "--admin ACCESS_REF SECRET_REF "
                "[--consumer NAME BUCKETS ACCESS_REF SECRET_REF] "
                "Key references are env:VARIABLE or file:PATH. Consumer buckets "
                "are comma- separated. All input is validated before the output is "
                "replaced. options: -h, --help show this help message and exit "
                "--out OUT --admin ACCESS_REF SECRET_REF "
                "--consumer NAME BUCKETS ACCESS_REF SECRET_REF "
                "repeat for each consumer, including o11y"
            ),
        ),
    ],
)
def test_help_explains_key_references(
    args: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args([*args, "--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert " ".join(captured.out.split()) == expected
    assert captured.err == ""


def test_top_level_help_lists_s3(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["--help"])
    assert exc.value.code == 0
    assert "s3 prepare the production object store" in " ".join(
        capsys.readouterr().out.split()
    )


@pytest.mark.parametrize(
    "key", ["ADMIN_ACCESS", "ADMIN_SECRET", "CLIENT_ACCESS", "CLIENT_SECRET"]
)
def test_missing_key_preserves_output(tmp_path: Path, key: str) -> None:
    target = tmp_path / "out"
    target.write_text("old")
    env = {k: v for k, v in ENV.items() if k != key}
    assert invoke(
        target,
        "--admin",
        "env:ADMIN_ACCESS",
        "env:ADMIN_SECRET",
        "--consumer",
        "o11y",
        "loki",
        "env:CLIENT_ACCESS",
        "env:CLIENT_SECRET",
        env=env,
    ) == (
        1,
        "",
        (
            "tbhbops s3 render-identities: identity rendering failed; "
            "check inputs and output permissions\n"
        ),
    )
    assert target.read_text() == "old"


@pytest.mark.parametrize("case", ["missing-file", "missing-parent", "invalid-bucket"])
def test_safe_failures(tmp_path: Path, case: str) -> None:
    target = tmp_path / "out"
    args = ["--admin", "env:ADMIN_ACCESS", "env:ADMIN_SECRET"]
    if case == "missing-file":
        args[1] = f"file:{tmp_path / 'EXAMPLESECRET'}"
    elif case == "missing-parent":
        target = tmp_path / "EXAMPLESECRET" / "out"
    else:
        args += [
            "--consumer",
            "o11y",
            "EXAMPLESECRET",
            "env:CLIENT_ACCESS",
            "env:CLIENT_SECRET",
        ]
    assert invoke(target, *args) == (
        1,
        "",
        (
            "tbhbops s3 render-identities: identity rendering failed; "
            "check inputs and output permissions\n"
        ),
    )
    assert not target.exists()
