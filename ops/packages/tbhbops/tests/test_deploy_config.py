"""Only the reviewed host allow-list schema can configure root operations."""

import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from tbhbops.deploy import config
from tbhbops.deploy.protocol import Json, RefusalError

from .test_deploy_filesystem import trusted_tree as trusted_tree  # noqa: PLC0414  # pytest fixture registration


def document() -> dict[str, Json]:
    return {
        "format": 1,
        "service": "o11y",
        "root": "/srv/o11y",
        "compose_file": "compose.yaml",
        "compose_env_file": "compose.env",
        "min_free_gib": 8,
        "max_stream_mib": 64,
        "runc_exceptions": [],
        "images": ["registry.example/web"],
        "ro_binds": [],
        "init": ["/opt/tbhb-deploy/current/bin/tbhbops", "o11y", "init"],
        "health": {
            "timeout_s": 10,
            "services": ["web"],
            "probes": [{"service": "web", "argv": ["true"]}],
        },
        "ports": [{"host_ip": "127.0.0.1", "port": 8080, "protocol": "tcp"}],
        "networks": [{"key": "default", "subnet": "203.0.113.32/24"}],
        "secrets": [
            {
                "name": "token",
                "op": "op:\x2f/o11y/item/field",
                "target": "data/secrets/token",
                "mode": 256,
                "uid": 0,
                "gid": 0,
            }
        ],
    }


@pytest.fixture
def deploy_service() -> config.Service:
    return config.service(document(), "o11y")


def test_service_preserves_every_setting(deploy_service: config.Service) -> None:
    assert deploy_service == config.Service(
        "o11y",
        Path("/srv/o11y"),
        "compose.yaml",
        "compose.env",
        8,
        64,
        ("/opt/tbhb-deploy/current/bin/tbhbops", "o11y", "init"),
        (),
        10,
        ("web",),
        (("web", ("true",)),),
        (("127.0.0.1", 8080, "tcp"),),
        {"default": "203.0.113.32/24"},
        (
            config.Secret(
                "token",
                "op:\x2f/o11y/item/field",
                Path("/srv/o11y/data/secrets/token"),
                256,
                0,
                0,
            ),
        ),
        ("registry.example/web",),
        (),
    )
    model = document()
    model.update(min_free_gib=0, max_stream_mib=1, init=[], runc_exceptions=["db"])
    model["health"] = {
        "timeout_s": 1,
        "services": ["db", "web"],
        "probes": [
            {"service": "db", "argv": ["/probe", "--ready"]},
            {"service": "web", "argv": ["true"]},
        ],
    }
    model["ports"] = [
        {"host_ip": "::1", "port": 1, "protocol": "udp"},
        {"host_ip": "127.0.0.1", "port": 65535, "protocol": "tcp"},
    ]
    configured = config.service(model, "o11y")
    assert configured.min_free_gib == 0
    assert configured.max_stream_mib == configured.timeout_s == 1
    assert configured.init == ()
    assert configured.runc_exceptions == ("db",)
    assert configured.health_services == ("db", "web")
    assert configured.probes == (("db", ("/probe", "--ready")), ("web", ("true",)))
    assert configured.ports == (("::1", 1, "udp"), ("127.0.0.1", 65535, "tcp"))
    del model["networks"]
    assert config.service(model, "o11y").networks == {}


@pytest.mark.parametrize("value", [None, False, 0, 1.5, [], {}])
def test_text_requires_string(value: Json) -> None:
    with pytest.raises(RefusalError, match=r"^expected string$"):
        config.text(value)


def test_retained_limit_default_and_override() -> None:
    model = document()
    assert config.service(model, "o11y").max_retained_mib == 512
    model["max_retained_mib"] = 1
    assert config.service(model, "o11y").max_retained_mib == 1
    model["max_retained_mib"] = 0
    with pytest.raises(RefusalError, match=r"^integer below minimum$"):
        config.service(model, "o11y")


@pytest.mark.parametrize("value", ["", "Mixed text", "é"])
def test_text_preserves_string(value: str) -> None:
    assert config.text(value) == value


@pytest.mark.parametrize("value", [None, False, True, 1.0, "1", [], {}])
def test_integer_requires_exact_type(value: Json) -> None:
    with pytest.raises(RefusalError, match=r"^expected integer$"):
        config.integer(value)


@pytest.mark.parametrize("minimum", [-1, 0, 1, 65535])
def test_integer_boundary(minimum: int) -> None:
    assert config.integer(minimum, minimum) == minimum
    assert config.integer(minimum + 1, minimum) == minimum + 1
    with pytest.raises(RefusalError, match=r"^integer below minimum$"):
        config.integer(minimum - 1, minimum)


def test_rows_preserve_all_tables() -> None:
    assert config.rows([]) == []
    assert config.rows([{"a": 1}, {"b": "two"}]) == [{"a": 1}, {"b": "two"}]
    with pytest.raises(RefusalError, match=r"^expected table list$"):
        config.rows({})
    with pytest.raises(RefusalError, match=r"^expected an object$"):
        config.rows([{}, 1])


@pytest.mark.parametrize("name", ["x", "a" + "0" * 31, "a-b9"])
def test_service_name_boundaries(name: str) -> None:
    model = document()
    model.update(service=name, root="/srv/" + name)
    assert config.service(model, name).name == name


@pytest.mark.parametrize("name", ["", "a" * 33, "1web", "Web", "web_", "web\n"])
def test_service_name_refusal(name: str) -> None:
    model = document()
    model.update(service=name, root="/srv/" + name)
    with pytest.raises(RefusalError, match=r"^service name$"):
        config.service(model, name)


@pytest.mark.parametrize("key", ["name", "op", "target", "mode", "uid", "gid"])
def test_secret_requires_each_key(key: str) -> None:
    row = config.rows(document()["secrets"])[0]
    del row[key]
    with pytest.raises(RefusalError, match=r"^secret keys$"):
        config.secret(row, Path("/srv/o11y"))


@pytest.mark.parametrize("mode", [0, 0o400, 0o755])
def test_secret_metadata_is_preserved(mode: int) -> None:
    row: dict[str, Json] = {
        "name": "Token_1.txt",
        "op": "op:\x2f/vault/item/field",
        "target": "data/custom/key",
        "mode": mode,
        "uid": 123,
        "gid": 456,
    }
    assert config.secret(row, Path("/srv/custom")) == config.Secret(
        "Token_1.txt",
        "op:\x2f/vault/item/field",
        Path("/srv/custom/data/custom/key"),
        mode,
        123,
        456,
    )


@pytest.mark.parametrize("target", ["deploy/key", "incoming/key", "releases/key"])
def test_secret_refuses_each_control_directory(target: str) -> None:
    row = config.rows(document()["secrets"])[0]
    row["target"] = target
    with pytest.raises(RefusalError, match=r"^secret control target$"):
        config.secret(row, Path("/srv/o11y"))


def test_service_schema(deploy_service: config.Service, trusted_tree: Path) -> None:
    assert deploy_service.root == Path("/srv/o11y")
    assert deploy_service.secrets[0].target == Path("/srv/o11y/data/secrets/token")
    assert deploy_service.probes == (("web", ("true",)),)
    assert deploy_service.networks == {"default": "203.0.113.32/24"}
    model = document()
    del model["init"]
    del model["networks"]
    assert config.service(model, "o11y").init == ()
    file = trusted_tree / "service.toml"
    file.write_text('name = "o11y"\n')
    assert config.load(file) == {"name": "o11y"}


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("unknown", 1, "unknown object key"),
        ("format", True, "expected integer"),
        ("format", 2, "service format"),
        ("service", "other", "service name"),
        ("root", "/srv/other", "service root"),
        ("root", 1, "expected string"),
        ("compose_file", "../x", "parent member path"),
        ("compose_file", "./x", "compose path"),
        ("compose_env_file", "/outside", "absolute member path"),
        ("min_free_gib", -1, "integer below minimum"),
        ("min_free_gib", True, "expected integer"),
        ("max_stream_mib", 0, "integer below minimum"),
        ("init", ["relative"], "init executable"),
        ("init", [1], "expected strings"),
        ("init", {}, "expected a string list"),
        ("runc_exceptions", [False], "expected strings"),
        ("health", {"services": []}, "health keys"),
        (
            "health",
            {"timeout_s": 0, "services": ["web"], "probes": []},
            "integer below minimum",
        ),
        ("health", {"timeout_s": 1, "services": [], "probes": []}, "health services"),
        (
            "health",
            {"timeout_s": 1, "services": ["web", "web"], "probes": []},
            "health services",
        ),
        (
            "ports",
            [{"host_ip": "127.0.0.1", "port": 65536, "protocol": "tcp"}],
            "port range or protocol",
        ),
        (
            "ports",
            [{"host_ip": "0.0.0.0", "port": 8080, "protocol": "tcp"}],  # noqa: S104  # refusal case
            "unspecified port address",
        ),
        (
            "ports",
            [{"host_ip": "127.0.0.1", "port": 8080, "protocol": "sctp"}],
            "port range or protocol",
        ),
        (
            "ports",
            [{"host_ip": "127.0.0.1", "port": 8080, "protocol": "tcp", "extra": 1}],
            "port keys",
        ),
        (
            "networks",
            [{"key": "x", "subnet": "bad"}],
            "'bad' does not appear to be an IPv4 or IPv6 network",
        ),
        ("networks", [{"key": "x"}], "network keys"),
        (
            "networks",
            [
                {"key": "x", "subnet": "203.0.113.32/24"},
                {"key": "x", "subnet": "203.0.113.32/24"},
            ],
            "duplicate network",
        ),
        ("secrets", [1], "expected an object"),
        ("secrets", {}, "expected table list"),
    ],
)
def test_service_refuses(key: str, value: Json, reason: str) -> None:
    model = document()
    model[key] = value
    with pytest.raises((RefusalError, ValueError)) as caught:
        config.service(model, "o11y")
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("name", "../token", "secret name"),
        ("op", "plain", "secret reference"),
        ("target", "token", "secret target"),
        ("target", "deploy/token", "secret control target"),
        ("target", "data/../token", "parent member path"),
        ("mode", 0o1000, "secret mode"),
        ("mode", 4095, "secret mode"),
        ("mode", 438, "secret mode"),
        ("uid", -1, "integer below minimum"),
        ("extra", 1, "unknown object key"),
    ],
)
def test_secret_refuses(key: str, value: Json, reason: str) -> None:
    model: dict[str, Json] = {
        "name": "token",
        "op": "op:\x2f/o11y/item/field",
        "target": "data/secrets/token",
        "mode": 0o400,
        "uid": 0,
        "gid": 0,
    }
    model[key] = value
    with pytest.raises(RefusalError) as caught:
        config.secret(model, Path("/srv/o11y"))
    assert str(caught.value) == reason


def test_required_and_unique_fields() -> None:
    model = document()
    del model["format"]
    with pytest.raises(RefusalError):
        config.service(model, "o11y")
    with pytest.raises(RefusalError):
        config.secret({}, Path("/srv/o11y"))
    for duplicate in ("name", "target"):
        model = document()
        first: dict[str, Json] = {
            "name": "a",
            "op": "op:\x2f/o11y/item/field",
            "target": "data/secrets/a",
            "mode": 0o400,
            "uid": 0,
            "gid": 0,
        }
        second = deepcopy(first)
        second["name" if duplicate == "target" else "target"] = (
            "b" if duplicate == "target" else "data/secrets/b"
        )
        model["secrets"] = [first, second]
        with pytest.raises(RefusalError):
            config.service(model, "o11y")
    probes: list[dict[str, Json]] = [
        {"service": "other", "argv": ["true"]},
        {"service": "web", "argv": []},
        {"service": "web"},
    ]
    for probe in probes:
        model = document()
        model["health"] = {"timeout_s": 1, "services": ["web"], "probes": [probe]}
        with pytest.raises(RefusalError):
            config.service(model, "o11y")


@pytest.mark.parametrize(
    "key",
    [
        "format",
        "service",
        "root",
        "compose_file",
        "compose_env_file",
        "min_free_gib",
        "max_stream_mib",
        "runc_exceptions",
        "images",
        "ro_binds",
        "health",
        "ports",
        "secrets",
    ],
)
def test_service_requires_every_mandatory_key(key: str) -> None:
    model = document()
    del model[key]
    with pytest.raises(RefusalError, match=r"^missing service key$"):
        config.service(model, "o11y")


@pytest.mark.parametrize(
    "reference", ["op:\x2f/vault/item", "OP://vault/item/field", "op:/vault/item/field"]
)
def test_secret_reference_prefix_and_components(reference: str) -> None:
    row = config.rows(document()["secrets"])[0]
    row["op"] = reference
    with pytest.raises(RefusalError, match=r"^secret reference$"):
        config.secret(row, Path("/srv/o11y"))


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        ({"service": "web"}, "probe keys"),
        ({"service": "web", "argv": ["true"], "extra": 1}, "probe keys"),
        ({"service": "other", "argv": ["true"]}, "probe target or arguments"),
        ({"service": "web", "argv": []}, "probe target or arguments"),
    ],
)
def test_probe_refusal_reasons(probe: dict[str, Json], reason: str) -> None:
    model = document()
    model["health"] = {"timeout_s": 1, "services": ["web"], "probes": [probe]}
    with pytest.raises(RefusalError) as caught:
        config.service(model, "o11y")
    assert str(caught.value) == reason


def test_health_rejects_unknown_fields_before_missing_fields() -> None:
    model = document()
    model["health"] = {"timeout_s": 1, "services": ["web"], "extra": 1}
    with pytest.raises(RefusalError, match=r"^unknown object key$"):
        config.service(model, "o11y")


def test_zero_port_is_below_service_minimum() -> None:
    model = document()
    model["ports"] = [{"host_ip": "::1", "port": 0, "protocol": "tcp"}]
    with pytest.raises(RefusalError, match=r"^integer below minimum$"):
        config.service(model, "o11y")


@pytest.mark.parametrize("duplicate", ["name", "target"])
def test_duplicate_secret_refusal_reasons(duplicate: str) -> None:
    model = document()
    first = config.rows(model["secrets"])[0]
    second = dict(first)
    second["name" if duplicate == "target" else "target"] = (
        "second" if duplicate == "target" else "data/secrets/second"
    )
    model["secrets"] = [first, second]
    with pytest.raises(RefusalError) as caught:
        config.service(model, "o11y")
    assert str(caught.value) == f"duplicate secret {duplicate}"


@pytest.mark.parametrize(
    "image",
    [
        "web",
        "example/web",
        "registry.example/web:tag",
        "registry.example/web@sha256:abc",
        "registry.example/",
        "registry.example/Web",
    ],
)
def test_image_repository_allow_list_spelling(image: str) -> None:
    model = document()
    model["images"] = [image]
    with pytest.raises(RefusalError):
        config.service(model, "o11y")


@pytest.mark.parametrize(
    "image",
    [
        "registry.example/web",
        "localhost/web",
        "localhost/team/web",
        "localhost:5000/team/web",
        "registry.example:5000/web",
    ],
)
def test_qualified_repository_allow_list(image: str) -> None:
    model = document()
    model["images"] = [image]
    assert config.service(model, "o11y").images == (image,)


@pytest.mark.parametrize(
    "path",
    [
        "/srv/o11y",
        "/srv/other/data",
        "/srv/o11y/deploy/key",
        "/srv/o11y/incoming/key",
        "/srv/o11y/releases/key",
        "/srv/o11y/../other",
        "data/file",
    ],
)
def test_read_only_allow_list_paths_refuse(path: str) -> None:
    model = document()
    model["ro_binds"] = [path]
    with pytest.raises(RefusalError):
        config.service(model, "o11y")


def test_read_only_allow_list_preserves_exact_paths() -> None:
    model = document()
    model["ro_binds"] = ["/srv/o11y/data/config", "/srv/o11y/data/secrets/generated"]
    assert config.service(model, "o11y").ro_binds == (
        Path("/srv/o11y/data/config"),
        Path("/srv/o11y/data/secrets/generated"),
    )


@pytest.mark.parametrize("key", ["images", "ro_binds"])
@pytest.mark.parametrize("value", [None, "text", [1]])
def test_allow_lists_require_string_arrays(key: str, value: Json) -> None:
    model = document()
    model[key] = value
    with pytest.raises(RefusalError):
        config.service(model, "o11y")


@pytest.mark.parametrize("name", ["receiver.toml", "o11y.toml"])
@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("symlink", "symlink path"),
        ("parent-link", "control path symlink or non-directory"),
        ("owner", "control file owner"),
        ("writable", "writable control file"),
    ],
)
def test_load_refuses_untrusted_toml(
    trusted_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    kind: str,
    reason: str,
) -> None:
    file = trusted_tree / name
    file.write_text('name = "o11y"\n')
    if kind == "symlink":
        link = trusted_tree / "link"
        link.symlink_to(file)
        file = link
    elif kind == "parent-link":
        link = trusted_tree / "link"
        link.symlink_to(trusted_tree, target_is_directory=True)
        file = link / name
    elif kind == "writable":
        file.chmod(0o622)
    else:
        original = os.fstat

        def wrong_owner(fd: int) -> os.stat_result:
            info = original(fd)
            fields = list(info)
            if stat.S_ISREG(info.st_mode):
                fields[4] = 1
            return os.stat_result(fields)

        monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        config.load(file)


def test_secret_rejects_backup_metadata_name() -> None:
    row = config.rows(document()["secrets"])[0]
    row["name"] = "metadata.json"
    with pytest.raises(RefusalError, match=r"^reserved secret name$"):
        config.secret(row, Path("/srv/o11y"))


@pytest.mark.parametrize(
    ("uid", "mode", "reason"),
    [
        (1, 0o755, "directory owner"),
        (0, 0o775, "writable directory"),
        (0, 0o757, "writable directory"),
    ],
)
def test_load_checks_each_parent_directory(
    trusted_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    mode: int,
    reason: str,
) -> None:
    directory = trusted_tree / "configuration"
    directory.mkdir()
    file = directory / "receiver.toml"
    file.write_text('compose_version = "5.7.1"\n')
    inode = directory.stat().st_ino
    original = os.fstat

    def untrusted(fd: int) -> os.stat_result:
        actual = original(fd)
        fields = list(actual)
        if actual.st_ino == inode:
            fields[4] = uid
            fields[0] = stat.S_IFDIR | mode
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", untrusted)
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        config.load(file)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("example/team.with.dot/web", "image registry host"),
        ("registry.example/web:tag", "image repository spelling"),
    ],
)
def test_repository_refusal_reasons(value: str, reason: str) -> None:
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        config.repository(value)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("/srv/other/data", "read-only bind outside root"),
        ("/srv/o11y/deploy/file", "read-only bind control path"),
    ],
)
def test_read_only_allow_list_refusal_reasons(value: str, reason: str) -> None:
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        config.ro_bind(value, Path("/srv/o11y"))


@pytest.mark.parametrize("path", ["/srv/o11y", "/srv/o11y/init", "/srv/o11y/bin/init"])
def test_init_cannot_occupy_service_root(path: str) -> None:
    model = document()
    model["init"] = [path]
    with pytest.raises(RefusalError, match=r"^init inside service root$"):
        config.service(model, "o11y")


@pytest.mark.parametrize("path", ["/srv/o11y-other/init", "/opt/deploy/init"])
def test_init_outside_service_root(path: str) -> None:
    model = document()
    model["init"] = [path]
    assert config.service(model, "o11y").init == (path,)


def test_sender_reads_user_owned_configuration(tmp_path: Path) -> None:
    path = tmp_path / "service.toml"
    path.write_text('format = 1\nservice = "metrics"\n')
    assert config.load_sender(path) == {"format": 1, "service": "metrics"}


@pytest.mark.parametrize(
    "reference",
    [
        "op:\x2f/vault/item/field/extra",
        "op:\x2f/vault/item/section/field/extra",
        "op:\x2f/vault//field",
        "op:\x2f//item/field",
        "op:\x2f/vault/item/",
    ],
)
def test_secret_reference_matches_sender_components(reference: str) -> None:
    row = config.rows(document()["secrets"])[0]
    row["op"] = reference
    with pytest.raises(RefusalError, match=r"^secret reference$"):
        config.secret(row, Path("/srv/o11y"))
