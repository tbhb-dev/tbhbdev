"""Runner commands never need a shell or a plaintext secret file."""

import argparse
import io
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock, call, patch
lazy from pathlib import Path

import pytest
from tbhbops_cli import cli, connect, deploy

from tbhbops.deploy import config
from tbhbops.deploy.config import Secret, Service
from tbhbops.deploy.protocol import RefusalError, Request, unpack

RELEASE = "a" * 40


@pytest.fixture
def system(tmp_path: Path) -> cli.System:
    def http(
        _method: str, _url: str, _headers: object, _body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, b"[]"

    def run(_argv: list[str]) -> str:
        return ""

    return cli.System(
        {"OP_CONNECT_HOST": "https://example.invalid", "OP_CONNECT_TOKEN": "fixture"},
        http,
        run,
        tmp_path,
        tmp_path / "words",
        datetime(2026, 11, 13, tzinfo=UTC),
        "test-host",
    )


def test_receive_cli(system: cli.System, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []

    def receive(_out: object, *, rollback: str | None = None) -> int:
        calls.append(rollback)
        return 7

    monkeypatch.setattr(deploy, "receive", receive)
    cases: tuple[list[str], ...] = ([], ["--rollback", "o11y"])
    for arguments in cases:
        assert (
            cli.run(
                ["deploy", "receive", *arguments],
                system,
                io.StringIO(),
                io.StringIO(),
            )
            == 7
        )
    assert calls == [None, "o11y"]


def test_connect_reference(system: cli.System, monkeypatch: pytest.MonkeyPatch) -> None:
    data: list[object] = [
        [{"id": "v1", "name": "o11y"}],
        [{"id": "i1", "title": "item"}],
        {"fields": [{"id": "field", "label": "label", "value": "fixture"}]},
    ]

    def call(_self: connect.Connect, _method: str, _path: str, _what: str) -> object:
        return data.pop(0)

    monkeypatch.setattr(connect.Connect, "call", call)
    server = connect.Connect(system.http, "https://example.invalid", "fixture")
    assert deploy.resolve(server, "op:\x2f/o11y/item/field") == b"fixture"
    for reference in ("plain", "op:\x2f/o11y/item", "op:\x2f/o11y//field"):
        with pytest.raises(RefusalError):
            deploy.resolve(server, reference)
    data[:] = [[]]
    with pytest.raises(RefusalError, match=r"^secret vault lookup$"):
        deploy.resolve(server, "op:\x2f/o11y/item/field")
    data[:] = [[{"id": "v1", "name": "o11y"}], []]
    with pytest.raises(RefusalError, match=r"^secret item lookup$"):
        deploy.resolve(server, "op:\x2f/o11y/item/field")
    data[:] = [
        [{"id": "v1", "name": "o11y"}],
        [{"id": "i1", "title": "item"}],
        {"fields": []},
    ]
    with pytest.raises(RefusalError, match=r"^secret field lookup$"):
        deploy.resolve(server, "op:\x2f/o11y/item/field")


def test_pack_cli(
    system: cli.System, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    service = tmp_path / "o11y.toml"
    service.write_text("""format = 1
service = "o11y"
root = "/srv/o11y"
compose_file = "compose.yaml"
compose_env_file = "compose.env"
min_free_gib = 8
max_stream_mib = 64
runc_exceptions = []
images = ["registry.example/web"]
ro_binds = []
ports = []
secrets = []
[health]
timeout_s = 10
services = ["web"]
probes = []
""")
    directory = tmp_path / "release"
    directory.mkdir()
    (directory / "config").mkdir()
    (directory / "config" / "web").write_bytes(b"config")
    args = [
        "deploy",
        "pack",
        "--service-file",
        str(service),
        "--release-dir",
        str(directory),
        "--service",
        "o11y",
        "--release",
        RELEASE,
        "--run-id",
        "123",
    ]
    assert cli.run(args, system, io.StringIO(), io.StringIO()) == 0
    data = capsysbinary.readouterr().out
    result = unpack(data, Request("o11y", RELEASE, "123"), set(), len(data))
    assert result.files == {"release/config/web": b"config"}
    err = io.StringIO()
    system = replace(system, env={"OP_CONNECT_HOST": "https://example.invalid"})
    assert cli.run(args, system, io.StringIO(), err) == 1
    assert "deploy command refused" in err.getvalue()
    with pytest.raises(RefusalError, match=r"^release directory$"):
        deploy.files(tmp_path / "missing")
    (directory / "link").symlink_to(directory / "config" / "web")
    with pytest.raises(RefusalError, match=r"^release entry type$"):
        deploy.files(directory)


def test_pack_loads_sender_config_from_user_owned_checkout(
    system: cli.System, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert tmp_path.stat().st_uid != 0
    service_file = tmp_path / "service.toml"
    service_file.write_text("""format = 1
service = "metrics"
root = "/srv/metrics"
compose_file = "compose.yaml"
compose_env_file = "compose.env"
min_free_gib = 0
max_stream_mib = 1
runc_exceptions = []
images = []
ro_binds = []
ports = []
secrets = []
health = { timeout_s = 1, services = ["web"], probes = [] }
""")
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    (release_dir / "compose.yaml").write_bytes(b"services: {}\n")
    with pytest.raises(RefusalError, match=r"^(directory owner|writable directory)$"):
        config.load(service_file)
    error = io.StringIO()
    status = cli.run(
        [
            "deploy",
            "pack",
            "--service-file",
            str(service_file),
            "--release-dir",
            str(release_dir),
            "--service",
            "metrics",
            "--release",
            RELEASE,
        ],
        system,
        io.StringIO(),
        error,
    )
    assert status == 0, error.getvalue()
    assert error.getvalue() == ""
    body = capsysbinary.readouterr().out
    packed = unpack(body, Request("metrics", RELEASE, None), set(), len(body))
    assert packed.files == {"release/compose.yaml": b"services: {}\n"}


@pytest.mark.parametrize(
    ("status", "outcome", "expected"), [(0, "ok", 0), (1, "ok", 1), (0, "failed", 1)]
)
def test_send_cli(
    system: cli.System,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    outcome: str,
    expected: int,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        record = {
            "stack": "o11y",
            "release": RELEASE,
            "outcome": outcome,
            "step": "done",
            "duration_s": 1.2,
            "run_id": "123",
        }
        return subprocess.CompletedProcess(
            argv, status, b"progress\n" + json.dumps(record).encode(), b""
        )

    monkeypatch.setattr(subprocess, "run", run)
    output = io.StringIO()
    args = [
        "deploy",
        "send",
        "--service",
        "o11y",
        "--release",
        RELEASE,
        "--host",
        "host-c",
        "--identity",
        "/job/key",
        "--known-hosts",
        "/job/known-hosts",
        "--run-id",
        "123",
    ]
    assert cli.run(args, system, output, io.StringIO()) == expected
    assert calls[0][-2:] == ["deploy@host-c", "o11y " + RELEASE + " 123"]
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert json.loads(output.getvalue())["outcome"] == outcome
    assert "progress" not in output.getvalue()


@pytest.mark.parametrize("host", ["-option", "host name", "x;command", ""])
def test_send_refuses_host(host: str) -> None:
    args = cli.parser().parse_args(
        [
            "deploy",
            "send",
            "--service",
            "o11y",
            "--release",
            RELEASE,
            "--host=" + host,
            "--identity",
            "/job/key",
            "--known-hosts",
            "/job/known-hosts",
        ]
    )
    with (
        patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("invalid host reached transport"),
        ) as transport,
        pytest.raises(RefusalError, match=r"^target hostname$"),
    ):
        deploy.send(args, io.StringIO())
    transport.assert_not_called()


def test_send_requires_matching_result(monkeypatch: pytest.MonkeyPatch) -> None:
    args = cli.parser().parse_args(
        [
            "deploy",
            "send",
            "--service",
            "o11y",
            "--release",
            RELEASE,
            "--host",
            "host-c",
            "--identity",
            "/job/key",
            "--known-hosts",
            "/job/known-hosts",
        ]
    )
    bodies: list[bytes] = [b"", b"{}"]

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, bodies.pop(0), b"")

    monkeypatch.setattr(subprocess, "run", run)
    for _ in range(2):
        with pytest.raises(RefusalError):
            deploy.send(args, io.StringIO())


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            None,
            (
                "usage: ops deploy [-h] {receive,pack,send} ...\n"
                "\n"
                "positional arguments:\n"
                "  {receive,pack,send}\n"
                "    receive            the root deploy receiver\n"
                "    pack               write a PAX release stream to stdout\n"
                "    send               stream stdin to deploy@host\n"
                "\n"
                "options:\n"
                "  -h, --help           show this help message and exit\n"
            ),
        ),
        (
            "receive",
            (
                "usage: ops deploy receive [-h] [--rollback SERVICE]\n"
                "\n"
                "options:\n"
                "  -h, --help          show this help message and exit\n"
                "  --rollback SERVICE\n"
            ),
        ),
        (
            "pack",
            (
                "usage: ops deploy pack [-h] --service-file SERVICE_FILE\n"
                "                       --release-dir RELEASE_DIR --service SERVICE\n"
                "                       --release RELEASE [--run-id RUN_ID]\n"
                "\n"
                "options:\n"
                "  -h, --help            show this help message and exit\n"
                "  --service-file SERVICE_FILE\n"
                "  --release-dir RELEASE_DIR\n"
                "  --service SERVICE\n"
                "  --release RELEASE\n"
                "  --run-id RUN_ID\n"
            ),
        ),
        (
            "send",
            (
                "usage: ops deploy send [-h] --service SERVICE --release RELEASE\n"
                "                       [--run-id RUN_ID] --host HOST "
                "--identity IDENTITY\n"
                "                       --known-hosts KNOWN_HOSTS\n"
                "\n"
                "options:\n"
                "  -h, --help            show this help message and exit\n"
                "  --service SERVICE\n"
                "  --release RELEASE\n"
                "  --run-id RUN_ID\n"
                "  --host HOST\n"
                "  --identity IDENTITY\n"
                "  --known-hosts KNOWN_HOSTS\n"
            ),
        ),
    ],
)
def test_deploy_help_contract(command: str | None, expected: str) -> None:
    root = argparse.ArgumentParser(prog="ops")
    groups = root.add_subparsers(dest="area", required=True)
    deploy.parser(groups)
    output = io.StringIO()
    arguments = ["deploy"] + ([command] if command is not None else []) + ["--help"]
    with patch("sys.stdout", output), pytest.raises(SystemExit) as caught:
        root.parse_args(arguments)
    assert caught.value.code == 0
    assert output.getvalue() == expected


@pytest.mark.parametrize("run_id", [None, "007"])
@pytest.mark.parametrize(
    ("status", "outcome", "expected"),
    [(0, "ok", 0), (0, "failed", 1), (2, "ok", 1), (2, "failed", 1)],
)
def test_send_exact_transport(
    monkeypatch: pytest.MonkeyPatch,
    run_id: str | None,
    status: int,
    outcome: str,
    expected: int,
) -> None:
    args = argparse.Namespace(
        service="metrics",
        release=RELEASE,
        run_id=run_id,
        host="host-01.tailnet",
        identity=Path("/job/id"),
        known_hosts=Path("/job/hosts"),
    )
    record = {
        "stack": "metrics",
        "release": RELEASE,
        "outcome": outcome,
        "step": "health",
        "duration_s": 2.3,
        "run_id": run_id,
        "reason": "withheld",
        "arbitrary": "withheld",
    }
    input_bytes = io.BytesIO(b"tar bytes")
    stdin = Mock(buffer=input_bytes)
    monkeypatch.setattr(deploy.sys, "stdin", stdin)
    output = io.StringIO()
    with patch.object(
        subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            [], status, b"untrusted\n" + json.dumps(record).encode() + b"\n"
        ),
    ) as run:
        assert deploy.send(args, output) == expected
    remote = "metrics " + RELEASE + (" 007" if run_id else "")
    run.assert_called_once_with(
        [
            "ssh",
            "-F",
            "/dev/null",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "UserKnownHostsFile=/job/hosts",
            "-i",
            "/job/id",
            "deploy@host-01.tailnet",
            remote,
        ],
        stdin=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    assert (
        output.getvalue()
        == json.dumps(
            {
                key: record[key]
                for key in (
                    "stack",
                    "release",
                    "outcome",
                    "step",
                    "duration_s",
                    "run_id",
                )
            }
        )
        + "\n"
    )


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (b"", "missing receiver result"),
        (b'{"stack":"metrics","release":"wrong"}', "receiver result identity"),
        (
            json.dumps({"stack": "other", "release": RELEASE}).encode(),
            "receiver result identity",
        ),
        (b"[]", "expected an object"),
        (b"invalid", "invalid JSON"),
    ],
)
def test_send_result_refusals(record: bytes, reason: str) -> None:
    args = argparse.Namespace(
        service="metrics",
        release=RELEASE,
        run_id=None,
        host="h",
        identity=Path("key"),
        known_hosts=Path("hosts"),
    )
    output = io.StringIO()
    with (
        patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 0, record)
        ),
        pytest.raises(RefusalError) as caught,
    ):
        deploy.send(args, output)
    assert str(caught.value) == reason
    assert output.getvalue() == ""


@pytest.mark.parametrize("by_id", [False, True])
def test_connect_lookup_exact_requests(system: cli.System, by_id: bool) -> None:
    server = connect.Connect(system.http, "https://example.invalid", "fixture")
    vault, item, field = "vault", "item", "field"
    # References have exactly three slash-separated components; identifiers may
    # contain URL metacharacters, which must be quoted in Connect requests.
    vault, item, field = ("v ?", "i ?", "f ?") if by_id else (vault, item, field)
    responses: list[object] = [
        [{"id": "decoy", "name": "other"}, {"id": "v ?", "name": "vault"}],
        [{"id": "other", "title": "other"}, {"id": "i ?", "title": "item"}],
        {
            "fields": [
                {"id": "other", "label": "other", "value": "wrong"},
                {"id": "f ?", "label": "field", "value": "value ☃\n"},
            ]
        },
    ]
    with patch.object(connect.Connect, "call", side_effect=responses) as request_call:
        assert (
            deploy.resolve(server, f"op:\x2f/{vault}/{item}/{field}")
            == "value ☃\n".encode()
        )
    assert request_call.call_args_list == [
        call("GET", "/v1/vaults", "listing vaults"),
        call("GET", "/v1/vaults/v%20%3F/items", "listing items"),
        call("GET", "/v1/vaults/v%20%3F/items/i%20%3F", "reading item"),
    ]


@pytest.mark.parametrize(
    ("responses", "reason"),
    [
        ([{}], "vault listing"),
        (
            [[{"name": "vault", "id": "v"}, {"name": "vault", "id": "v2"}]],
            "secret vault lookup",
        ),
        ([[{"name": "vault", "id": "v"}], {}], "item listing"),
        (
            [
                [{"name": "vault", "id": "v"}],
                [{"title": "item", "id": "i"}, {"title": "item", "id": "i2"}],
            ],
            "secret item lookup",
        ),
        (
            [[{"name": "vault", "id": "v"}], [{"title": "item", "id": "i"}], {}],
            "item fields",
        ),
        (
            [
                [{"name": "vault", "id": "v"}],
                [{"title": "item", "id": "i"}],
                {"fields": {}},
            ],
            "item fields",
        ),
        (
            [
                [{"name": "vault", "id": "v"}],
                [{"title": "item", "id": "i"}],
                {
                    "fields": [
                        {"label": "field", "value": "a"},
                        {"label": "field", "value": "b"},
                    ]
                },
            ],
            "secret field lookup",
        ),
        (
            [
                [{"name": "vault", "id": "v"}],
                [{"title": "item", "id": "i"}],
                {"fields": [{"label": "field", "value": None}]},
            ],
            "expected string",
        ),
    ],
)
def test_connect_refuses_ambiguous_or_malformed_responses(
    system: cli.System, responses: list[object], reason: str
) -> None:
    server = connect.Connect(system.http, "https://example.invalid", "fixture")
    with (
        patch.object(connect.Connect, "call", side_effect=responses),
        pytest.raises(RefusalError) as caught,
    ):
        deploy.resolve(server, "op:\x2f/vault/item/field")
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    "reference",
    [
        "vault/item/field",
        "op:\x2f/vault/item",
        "op:\x2f/vault/item/field/extra",
        "op:\x2f//item/field",
        "op:\x2f/vault//field",
        "op:\x2f/vault/item/",
    ],
)
def test_reference_rejected_before_connect(system: cli.System, reference: str) -> None:
    with (
        patch.object(connect.Connect, "call") as request_call,
        pytest.raises(RefusalError, match=r"^secret reference$"),
    ):
        deploy.resolve(
            connect.Connect(system.http, "https://example.invalid", "fixture"),
            reference,
        )
    request_call.assert_not_called()


@pytest.mark.parametrize("run_id", [None, "12"])
@pytest.mark.parametrize("size", [1024**2, 1024**2 + 1])
def test_pack_dispatch_arguments_and_limit(
    system: cli.System, monkeypatch: pytest.MonkeyPatch, run_id: str | None, size: int
) -> None:
    secret = Secret(
        "key", "op:\x2f/vault/item/field", Path("/srv/metrics/data/key"), 0o400, 1, 2
    )
    allowed = Service(
        "metrics",
        Path("/srv/metrics"),
        "compose",
        "env",
        0,
        1,
        (),
        (),
        1,
        ("web",),
        (),
        (),
        {},
        (secret,),
        ("registry.example/web",),
        (),
    )
    args = argparse.Namespace(
        command="pack",
        service="metrics",
        release=RELEASE,
        run_id=run_id,
        service_file=system.cwd / "service.toml",
        release_dir=Path("release"),
    )
    args.service_file.write_text("format = 1\n")
    output = io.BytesIO()
    monkeypatch.setattr(deploy.sys, "stdout", Mock(buffer=output))
    body = b"x" * size
    with (
        patch.object(deploy, "service", return_value=allowed) as service,
        patch.object(
            deploy.connect, "host_url", return_value="https://connect.invalid"
        ) as host,
        patch.object(deploy.connect, "Connect") as server,
        patch.object(deploy, "resolve", return_value=b"fixture") as resolve,
        patch.object(
            deploy, "files", return_value={"release/config": b"data"}
        ) as files,
        patch.object(deploy, "pack", return_value=body) as pack,
    ):
        if size > 1024**2:
            with pytest.raises(cli.CliError, match=r"^deploy command refused$"):
                deploy.dispatch(args, system, io.StringIO())
            assert output.getvalue() == b""
        else:
            assert deploy.dispatch(args, system, io.StringIO()) == 0
            assert output.getvalue() == body
    service.assert_called_once_with({"format": 1}, "metrics")
    host.assert_called_once_with("https://example.invalid", system.run)
    server.assert_called_once_with(system.http, "https://connect.invalid", "fixture")
    resolve.assert_called_once_with(server.return_value, "op:\x2f/vault/item/field")
    files.assert_called_once_with(Path("release"))
    pack.assert_called_once_with(
        Request("metrics", RELEASE, run_id),
        {"release/config": b"data"},
        {"key": b"fixture"},
    )


@pytest.mark.parametrize("command", ["pack", "send"])
def test_parser_exact_paths_and_defaults(command: str) -> None:
    arguments = ["deploy", command, "--service", "metrics", "--release", RELEASE]
    expected: dict[str, object] = {
        "command": command,
        "service": "metrics",
        "release": RELEASE,
        "run_id": None,
    }
    if command == "pack":
        arguments.extend(["--service-file", "allow.toml", "--release-dir", "tree"])
        expected.update(service_file=Path("allow.toml"), release_dir=Path("tree"))
    else:
        arguments.extend(
            ["--host", "host-1", "--identity", "key", "--known-hosts", "hosts"]
        )
        expected.update(host="host-1", identity=Path("key"), known_hosts=Path("hosts"))
    parser = argparse.ArgumentParser(prog="ops")
    deploy.parser(parser.add_subparsers())
    assert vars(parser.parse_args(arguments)) == expected


@pytest.mark.parametrize(
    "missing", ["--service", "--release", "--host", "--identity", "--known-hosts"]
)
def test_send_requires_each_argument(
    missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [
        "deploy",
        "send",
        "--service",
        "metrics",
        "--release",
        RELEASE,
        "--host",
        "host-1",
        "--identity",
        "key",
        "--known-hosts",
        "hosts",
    ]
    index = arguments.index(missing)
    del arguments[index : index + 2]
    parser = argparse.ArgumentParser(prog="ops")
    deploy.parser(parser.add_subparsers())
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(arguments)
    assert caught.value.code == 2
    assert (
        f"the following arguments are required: {missing}\n" in capsys.readouterr().err
    )


def test_root_help_describes_deploy_commands() -> None:
    entries = [
        line.split(maxsplit=1)
        for line in cli.parser().format_help().splitlines()
        if line.split()[:1] == ["deploy"]
    ]
    assert entries == [["deploy", "pack, send, and receive service releases"]]


def test_deploy_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    parser = argparse.ArgumentParser(prog="ops")
    deploy.parser(parser.add_subparsers())
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["deploy"])
    assert caught.value.code == 2
    assert "the following arguments are required: command\n" in capsys.readouterr().err


def test_connect_quotes_slashes_in_resolved_identifiers(system: cli.System) -> None:
    server = connect.Connect(system.http, "https://example.invalid", "fixture")
    replies: list[object] = [
        [{"id": "v/X", "name": "vault"}],
        [{"id": "i/X", "title": "item"}],
        {"fields": [{"id": "field", "value": "fixture"}]},
    ]
    with patch.object(connect.Connect, "call", side_effect=replies) as request_call:
        assert deploy.resolve(server, "op:\x2f/vault/item/field") == b"fixture"
    assert request_call.call_args_list == [
        call("GET", "/v1/vaults", "listing vaults"),
        call("GET", "/v1/vaults/v%2FX/items", "listing items"),
        call("GET", "/v1/vaults/v%2FX/items/i%2FX", "reading item"),
    ]


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_release_root_refusal_reasons(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "release"
    if kind == "file":
        path.write_bytes(b"file")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
    with pytest.raises(RefusalError, match=r"^release directory$"):
        deploy.files(path)


def test_receive_dispatch_preserves_the_output_stream(system: cli.System) -> None:
    output = io.StringIO()
    with patch.object(deploy, "receive", return_value=7) as receive:
        assert (
            deploy.dispatch(
                argparse.Namespace(command="receive", rollback=None), system, output
            )
            == 7
        )
    receive.assert_called_once_with(output, rollback=None)
