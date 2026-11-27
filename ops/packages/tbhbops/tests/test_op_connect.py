import io
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from tbhbops import op_connect as oc

COMPOSE_FILE = next(
    path / "apps" / "op-connect" / "compose.yaml"
    for path in Path(__file__).resolve().parents
    if (path / "apps" / "op-connect" / "compose.yaml").is_file()
)
COMPOSE_TEXT = COMPOSE_FILE.read_text(encoding="utf-8")
DIR = "/srv/op-connect"
ENV = {oc.DIR_ENV: DIR}
COMPOSE_PREFIX = [
    "sudo",
    "-n",
    "/usr/local/bin/docker-compose",
    "-p",
    "op-connect",
    "-f",
    "/srv/op-connect/compose.yaml",
]


class Fake:
    """Records each command and answers from a list of canned outputs."""

    def __init__(self, *outputs: bytes) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.outputs = list(outputs)

    def __call__(self, argv: list[str], stdin: bytes | None) -> bytes:
        self.calls.append((argv, stdin))
        return self.outputs.pop(0) if self.outputs else b""


def remote(call: tuple[list[str], bytes | None]) -> list[str]:
    argv, _ = call
    assert argv[:3] == ["ssh", "--", "nas-a"]
    assert len(argv) == 4
    return shlex.split(argv[3])


def run_main(
    *argv: str, fake: Fake, env: dict[str, str] | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    settings = {
        "OP_CONNECT_TARGET": "nas-a",
        "OP_CONNECT_DEPLOY_ACCOUNT": "account.example.invalid",
        "OP_CONNECT_DEPLOY_VAULT": "example-vault",
        "OP_CONNECT_DEPLOY_ITEM": "Example credentials document",
        **(ENV if env is None else env),
    }
    status = oc.main(list(argv), settings, COMPOSE_TEXT, fake, out)
    return status, out.getvalue()


def test_the_compose_file_pins_every_image_by_digest() -> None:
    images = [
        line.split()[1]
        for line in COMPOSE_TEXT.splitlines()
        if line.strip().startswith("image:")
    ]
    assert len(images) == 3
    assert all("@sha256:" in image for image in images)


def test_the_compose_file_publishes_no_port() -> None:
    assert "ports:" not in COMPOSE_TEXT


def test_ts_image_reads_the_pinned_tailscale_image() -> None:
    assert oc.ts_image(COMPOSE_TEXT).startswith("tailscale/tailscale:v")
    assert oc.ts_image(COMPOSE_TEXT).count("@sha256:") == 1


def test_ts_image_needs_a_tailscale_image() -> None:
    with pytest.raises(ValueError, match="no tailscale/tailscale image"):
        oc.ts_image("image: busybox\n")


def test_ssh_quotes_the_remote_command() -> None:
    assert oc.ssh("nas-a", ["echo", "a b"]) == ["ssh", "--", "nas-a", "echo 'a b'"]


def test_compose_names_the_project_and_file() -> None:
    assert oc.compose(DIR, "up", "-d") == [*COMPOSE_PREFIX, "up", "-d"]


def test_utility_mounts_the_directory_without_a_network() -> None:
    assert shlex.join(oc.utility(DIR, "img", "true")) == (
        "sudo -n /usr/local/bin/docker run -i --rm --network none"
        " -v /srv/op-connect:/d --entrypoint sh img -c true"
    )


@pytest.mark.parametrize(
    ("logs", "expected"),
    [
        ("nothing here", None),
        (
            "To authenticate, visit:\n\n\thttps://login.tailscale.com/a/abc123\n",
            "https://login.tailscale.com/a/abc123",
        ),
        (
            "https://login.tailscale.com/a/old1 then https://login.tailscale.com/a/new2.",
            "https://login.tailscale.com/a/new2",
        ),
    ],
)
def test_login_url(logs: str, expected: str | None) -> None:
    assert oc.login_url(logs) == expected


def test_node_key() -> None:
    assert (
        oc.node_key(json.dumps({"Self": {"PublicKey": "nodekey:ab12"}}))
        == "nodekey:ab12"
    )


def test_node_key_rejects_other_keys() -> None:
    with pytest.raises(ValueError, match="no node key"):
        oc.node_key(json.dumps({"Self": {"PublicKey": "mkey:ab12"}}))


@pytest.mark.parametrize("data", [b"not json", b"[1]", b'"s"'])
def test_check_credentials_rejects_non_objects_without_echoing(data: bytes) -> None:
    with pytest.raises(
        TypeError, match=r"^the credentials document is not a JSON object$"
    ):
        oc.check_credentials(data)


def test_check_credentials_drops_the_parse_error_and_its_document() -> None:
    with pytest.raises(TypeError) as caught:
        oc.check_credentials(b'{"secret": ')
    assert caught.value.__suppress_context__
    assert caught.value.__cause__ is None


def test_check_credentials_accepts_an_object() -> None:
    oc.check_credentials(b'{"verifier": {}}')


def test_heartbeat_url() -> None:
    assert (
        oc.heartbeat_url('{"MagicDNSSuffix": "example.example.invalid"}')
        == "https://op-connect.example.example.invalid/heartbeat"
    )


def test_run_subprocess_returns_stdout_and_feeds_stdin() -> None:
    assert oc.run_subprocess(["cat"], b"hello") == b"hello"


def test_run_subprocess_raises_on_failure() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        oc.run_subprocess(["false"], None)


def test_the_parser_names_the_tool_and_its_steps() -> None:
    usage = oc.parser().format_usage()
    assert usage.startswith("usage: op-connect [-h] [--confirm CONFIRM]")
    assert "{preflight,stage,secrets,up,login,lock,serve,health}" in usage


def test_main_rejects_an_unknown_step() -> None:
    with pytest.raises(SystemExit):
        run_main("deploy", fake=Fake())


@pytest.mark.parametrize("env", [{}, {oc.DIR_ENV: "relative/dir"}])
def test_main_needs_an_absolute_directory(env: dict[str, str]) -> None:
    fake = Fake()
    status, out = run_main("preflight", fake=fake, env=env)
    assert status == 2
    assert (
        out
        == "set OP_CONNECT_DIR to the deploy directory's absolute path on nas-a\n"
    )
    assert fake.calls == []


@pytest.mark.parametrize("step", ["stage", "secrets", "up", "serve"])
@pytest.mark.parametrize("confirm", [[], ["--confirm", "preflight"]])
def test_steps_that_change_the_host_need_confirming(
    step: str, confirm: list[str]
) -> None:
    fake = Fake()
    status, out = run_main(step, *confirm, fake=fake)
    assert status == 2
    assert out == f"{step} changes nas-a; pass --confirm {step}\n"
    assert fake.calls == []


def test_preflight_only_reads() -> None:
    fake = Fake(b"24.0.2\n", b"2.20.1\n", b"bridge\n", b"no deploy directory yet\n")
    status, out = run_main("preflight", fake=fake)
    assert status == 0
    assert out == "24.0.2\n2.20.1\nbridge\nno deploy directory yet\n"
    assert [remote(call) for call in fake.calls] == [
        [
            "sudo",
            "-n",
            "/usr/local/bin/docker",
            "version",
            "--format",
            "{{.Server.Version}}",
        ],
        ["sudo", "-n", "/usr/local/bin/docker-compose", "version", "--short"],
        [
            "sudo",
            "-n",
            "/usr/local/bin/docker",
            "network",
            "ls",
            "--format",
            "{{.Name}}",
        ],
        [
            "sh",
            "-c",
            "ls -ld /srv/op-connect 2>/dev/null || echo 'no deploy directory yet'",
        ],
    ]
    assert all(stdin is None for _, stdin in fake.calls)


def test_utility_passes_extra_arguments_to_the_script() -> None:
    assert oc.utility(DIR, "img", "true", "sh", "x")[-4:] == ["-c", "true", "sh", "x"]


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("/srv/op-connect", ("/srv", "op-connect")),
        ("/srv/example-storage/docker/op-connect", ("/srv/example-storage/docker", "op-connect")),
    ],
)
def test_split_directory(directory: str, expected: tuple[str, str]) -> None:
    assert oc.split_directory(directory) == expected


@pytest.mark.parametrize(
    "directory", ["/", "/op-connect", "/srv/", "/srv/.", "/srv/.."]
)
def test_split_directory_refuses_a_name_that_is_not_one_component(
    directory: str,
) -> None:
    with pytest.raises(ValueError, match=oc.BAD_DIRECTORY):
        oc.split_directory(directory)


def test_the_mkdir_script_only_makes_the_named_directory() -> None:
    assert oc.MKDIR_SCRIPT == 'set -eu; mkdir -p "/d/$1"; chmod 700 "/d/$1"'


def test_stage_makes_the_directory_then_sends_the_compose_file() -> None:
    fake = Fake()
    status, out = run_main("stage", "--confirm", "stage", fake=fake)
    assert status == 0
    assert out == "staged compose.yaml in /srv/op-connect\n"
    mkdir, stage = fake.calls
    image = oc.ts_image(COMPOSE_TEXT)
    assert remote(mkdir) == oc.utility(
        "/srv", image, oc.MKDIR_SCRIPT, "sh", "op-connect"
    )
    assert mkdir[1] is None
    assert remote(stage) == oc.utility(DIR, image, oc.STAGE_SCRIPT)
    assert stage[1] == COMPOSE_TEXT.encode()


def test_stage_refuses_a_bad_directory_before_touching_the_host() -> None:
    fake = Fake()
    with pytest.raises(ValueError, match=oc.BAD_DIRECTORY):
        run_main(
            "stage", "--confirm", "stage", fake=fake, env={"OP_CONNECT_DIR": "/srv/.."}
        )
    assert fake.calls == []


def test_secrets_pipe_the_document_from_op_to_the_host() -> None:
    document = b'{"verifier": {"salt": "x"}}'
    fake = Fake(document)
    status, out = run_main("secrets", "--confirm", "secrets", fake=fake)
    assert status == 0
    assert out == "wrote the credentials file\n"
    assert "salt" not in out
    get, put = fake.calls
    assert get == (
        [
            "op",
            "--account",
            "account.example.invalid",
            "document",
            "get",
            "Example credentials document",
            "--vault",
            "example-vault",
        ],
        None,
    )
    assert remote(put) == oc.utility(DIR, oc.ts_image(COMPOSE_TEXT), oc.SECRETS_SCRIPT)
    assert put[1] == document
    assert b"salt" not in put[0][2].encode()


def test_secrets_stop_before_the_host_on_a_bad_document() -> None:
    fake = Fake(b"<html>")
    with pytest.raises(TypeError, match="not a JSON object"):
        run_main("secrets", "--confirm", "secrets", fake=fake)
    assert len(fake.calls) == 1


def test_up_starts_the_project() -> None:
    fake = Fake(b"started\n")
    status, out = run_main("up", "--confirm", "up", fake=fake)
    assert status == 0
    assert out == "started\n"
    assert [remote(call) for call in fake.calls] == [[*COMPOSE_PREFIX, "up", "-d"]]


STATUS_ARGS = [*COMPOSE_PREFIX, "exec", "-T", "ts", "tailscale", "status", "--json"]


def test_login_when_signed_in() -> None:
    fake = Fake(b'{"BackendState": "Running"}')
    status, out = run_main("login", fake=fake)
    assert status == 0
    assert out == "the sidecar is signed in\n"
    assert [remote(call) for call in fake.calls] == [STATUS_ARGS]


def test_login_prints_the_url() -> None:
    fake = Fake(
        b'{"BackendState": "NeedsLogin"}', b"visit https://login.tailscale.com/a/f00d\n"
    )
    status, out = run_main("login", fake=fake)
    assert status == 0
    assert (
        out
        == "open this URL and sign the sidecar in: https://login.tailscale.com/a/f00d\n"
    )
    assert [remote(call) for call in fake.calls] == [
        STATUS_ARGS,
        [*COMPOSE_PREFIX, "logs", "--no-color", "ts"],
    ]


def test_login_without_a_url_yet() -> None:
    fake = Fake(b'{"BackendState": "Starting"}', b"booting\n")
    status, out = run_main("login", fake=fake)
    assert status == 0
    assert out == "no login URL yet; state Starting\n"


def test_lock_prints_the_sign_command() -> None:
    fake = Fake(b'{"Self": {"PublicKey": "nodekey:beef"}}')
    status, out = run_main("lock", fake=fake)
    assert status == 0
    assert out == "run on an authorized signing host: tailscale lock sign nodekey:beef\n"
    assert [remote(call) for call in fake.calls] == [STATUS_ARGS]


def test_serve_points_the_service_at_api() -> None:
    fake = Fake()
    status, out = run_main("serve", "--confirm", "serve", fake=fake)
    assert status == 0
    assert out == "serving svc:op-connect on 443\n"
    assert [remote(call) for call in fake.calls] == [
        [
            *COMPOSE_PREFIX,
            "exec",
            "-T",
            "ts",
            "tailscale",
            "serve",
            "--service=svc:op-connect",
            "--https=443",
            "http://api:8080",
        ]
    ]


@pytest.mark.parametrize(
    ("found", "expected"),
    [("/usr/bin/tailscale", "/usr/bin/tailscale"), (None, oc.MACOS_TAILSCALE)],
)
def test_health_finds_the_local_tailscale(
    monkeypatch: pytest.MonkeyPatch, found: str | None, expected: str
) -> None:
    monkeypatch.setattr(oc.shutil, "which", {"tailscale": found}.get)
    fake = Fake(b"ok", b"ok", b'{"MagicDNSSuffix": "example.example.invalid"}', b".")
    run_main("health", fake=fake)
    assert fake.calls[2] == ([expected, "status", "--json"], None)


def test_health_checks_both_containers_and_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oc.shutil, "which", {"tailscale": "tailscale"}.get)
    fake = Fake(
        b'{"name":"api"}\n',
        b'{"name":"sync"}\n',
        b'{"MagicDNSSuffix": "example.example.invalid"}',
        b".\n",
    )
    status, out = run_main("health", fake=fake)
    assert status == 0
    assert out == 'api: {"name":"api"}\nsync: {"name":"sync"}\nsvc:op-connect: .\n'
    api, sync, local, curl = fake.calls
    sidecar = shlex.join([*COMPOSE_PREFIX, "exec", "-T", "ts", "wget", "-qO-"])
    assert api[0][3] == f"{sidecar} http://api:8080/health"
    assert sync[0][3] == f"{sidecar} http://sync:8080/health"
    assert local == (["tailscale", "status", "--json"], None)
    assert curl == (
        ["curl", "-fsS", "https://op-connect.example.example.invalid/heartbeat"],
        None,
    )


def test_scripts_keep_the_secret_private() -> None:
    assert oc.SECRETS_SCRIPT.startswith("set -eu; umask 077; ")
    assert 'chown 999:999 "$f"; chmod 400 "$f"; ' in oc.SECRETS_SCRIPT
    assert oc.SECRETS_SCRIPT.endswith(
        'mv -f "$f" /d/secrets/1password-credentials.json'
    )
    assert 'test -s "$f"' in oc.SECRETS_SCRIPT
    assert "test ! -d /d/secrets/1password-credentials.json; " in oc.SECRETS_SCRIPT
    assert "chown 999:999 /d/data /d/secrets; " in oc.STAGE_SCRIPT
    assert oc.STAGE_SCRIPT.endswith("mv -f /d/compose.yaml.tmp /d/compose.yaml")
