import re
from pathlib import Path
from typing import cast

import yaml

ROOT = next(
    path
    for path in Path(__file__).parents
    if (path / "apps/s3/compose.yaml").is_file()
)

DEFAULT_BIND = "${S3_BIND_ADDR:-127.0.0.1}:9000:9000/tcp"
# Test data: the wildcard values the check rejects.
WILDCARDS = {"", "0.0.0.0", "::", "[::]", "*"}  # noqa: S104


Mapping = dict[str, object]


def _map(value: object) -> Mapping:
    assert isinstance(value, dict)
    return cast("Mapping", value)


def _text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _mounts(service: Mapping) -> list[Mapping]:
    volumes = cast("list[object]", service["volumes"])
    return [_map(volume) for volume in volumes if isinstance(volume, dict)]


def _services() -> dict[str, Mapping]:
    primary = _map(yaml.safe_load((ROOT / "apps/s3/compose.yaml").read_text()))
    assert primary["name"] == "s3"
    services = {name: _map(value) for name, value in _map(primary["services"]).items()}
    assert set(services) == {"rustfs", "lego"}
    return services


def _rustfs() -> Mapping:
    return _services()["rustfs"]


def _environment(service: Mapping) -> Mapping:
    return _map(service["environment"])


def test_object_store_compose() -> None:
    rustfs = _rustfs()
    environment = _environment(rustfs)
    image = _text(rustfs["image"])
    assert re.fullmatch(r"rustfs/rustfs:1\.\d+\.\d+@sha256:[0-9a-f]{64}", image)
    assert rustfs["cap_drop"] == ["ALL"]
    assert rustfs.get("cap_add", []) == []
    assert rustfs["security_opt"] == ["no-new-privileges:true"]
    assert rustfs["user"] == "10001:10001"
    assert rustfs["ports"] == [DEFAULT_BIND]
    assert environment["RUSTFS_UNSUPPORTED_FS_POLICY"] == "fail"
    assert environment["RUSTFS_ACCESS_KEY_FILE"] == "/run/s3/rustfs.access_key"
    # A mount path, not a secret.
    secret_file = environment["RUSTFS_SECRET_KEY_FILE"]
    assert secret_file == "/run/s3/rustfs.secret_key"  # noqa: S105
    keys = [
        volume
        for volume in _mounts(rustfs)
        if _text(volume["source"]).startswith("${S3_SECRETS_DIR:-./secrets}/")
    ]
    assert [volume["source"] for volume in keys] == [
        "${S3_SECRETS_DIR:-./secrets}/rustfs.access_key",
        "${S3_SECRETS_DIR:-./secrets}/rustfs.secret_key",
    ]
    assert [volume["target"] for volume in keys] == [
        "/run/s3/rustfs.access_key",
        "/run/s3/rustfs.secret_key",
    ]
    for volume in keys:
        assert volume["read_only"] is True
        assert volume["bind"] == {"create_host_path": False}


def test_certificate_service_compose() -> None:
    lego = _services()["lego"]
    image = _text(lego["image"])
    assert re.fullmatch(r"goacme/lego:v5\.6\.0@sha256:[0-9a-f]{64}", image)
    assert lego["profiles"] == ["cert"]
    assert lego["cap_drop"] == ["ALL"]
    assert lego.get("cap_add", []) == []
    assert lego["security_opt"] == ["no-new-privileges:true"]
    token = [
        volume
        for volume in _mounts(lego)
        if volume["source"] == "${S3_SECRETS_DIR:-./secrets}/lego-dns-token"
    ]
    assert token == [
        {
            "type": "bind",
            "source": "${S3_SECRETS_DIR:-./secrets}/lego-dns-token",
            "target": "/run/s3/lego-dns-token",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    # A mount path, not a secret.
    token_file = _environment(lego)["CLOUDFLARE_DNS_API_TOKEN_FILE"]
    assert token_file == "/run/s3/lego-dns-token"  # noqa: S105


def test_rustfs_reloads_a_renewed_certificate() -> None:
    # RustFS 1.0.4 defaults to no reload, and lego replaces the files in place.
    assert _environment(_rustfs())["RUSTFS_TLS_RELOAD_ENABLE"] == "true"


def test_every_service_sets_a_process_and_log_limit() -> None:
    for service in _services().values():
        pids_limit = service["pids_limit"]
        assert isinstance(pids_limit, int)
        assert pids_limit >= 1
        logging = _map(service["logging"])
        assert logging["driver"] == "json-file"
        options = _map(logging["options"])
        assert re.fullmatch(r"[1-9][0-9]*(b|k|m|g)?", _text(options["max-size"]))
        assert re.fullmatch(r"[1-9][0-9]*", _text(options["max-file"]))


def test_default_bind_is_not_a_wildcard() -> None:
    ports = cast("list[object]", _rustfs()["ports"])
    match = re.fullmatch(r"\$\{S3_BIND_ADDR:-([^}]*)\}:9000:9000/tcp", _text(ports[0]))
    assert match is not None
    assert match.group(1) not in WILDCARDS
