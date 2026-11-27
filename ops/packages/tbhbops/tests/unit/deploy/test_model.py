"""Validate Compose projects against the configured allow-list."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from tbhbops.deploy import model, mounts
from tbhbops.deploy.protocol import Json, RefusalError, mapping
lazy from tbhbops.deploy.config import Service

from ...deploy_model import RELEASE, changed_kitchen, kitchen, minimal


def test_normalized_minimal(deploy_service: Service) -> None:
    assert model.validate(minimal(command=None, entrypoint=None), deploy_service) == ()
    assert (
        model.validate(minimal(command=["true"], entrypoint=["sh"]), deploy_service)
        == ()
    )
    assert (
        model.validate({**minimal(), "x-inert": {"privileged": True}}, deploy_service)
        == ()
    )


def test_all_normalized_forms(deploy_service: Service) -> None:
    sources = model.validate(kitchen(), deploy_service)
    assert len(sources) == 5
    assert sum(source.writable for source in sources) == 1
    writable = {source.path for source in sources if source.writable}
    mounts.paths(sources, (), writable, deploy_service, RELEASE)
    assert (
        model.validate(
            minimal(runtime="runc"), replace(deploy_service, runc_exceptions=("web",))
        )
        == ()
    )
    assert model.validate(minimal(cpus=1), deploy_service) == ()


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("privileged", "unknown object key"),
        ("cap_add", "unknown object key"),
        ("devices", "unknown object key"),
        ("device_cgroup_rules", "unknown object key"),
        ("pid", "unknown object key"),
        ("ipc", "unknown object key"),
        ("uts", "unknown object key"),
        ("userns_mode", "unknown object key"),
        ("network_mode", "unknown object key"),
        ("cgroup_parent", "unknown object key"),
        ("volumes_from", "unknown object key"),
        ("sysctls", "unknown object key"),
        ("build", "unknown object key"),
        ("gpus", "unknown object key"),
        ("dns", "unknown object key"),
        ("profiles", "unknown object key"),
        ("unknown", "unknown object key"),
    ],
)
def test_forbidden_service_keys(deploy_service: Service, key: str, reason: str) -> None:
    with pytest.raises(RefusalError) as caught:
        model.validate(minimal(**{key: True}), deploy_service)
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"runtime": "runc"}, "runtime"),
        ({"runtime": "other"}, "runtime"),
        ({"security_opt": ["seccomp=unconfined"]}, "security option"),
        ({"image": "example:latest"}, "image digest"),
        ({"command": "sh"}, "expected a string list"),
        ({"entrypoint": 1}, "expected a string list"),
        ({"cpus": True}, "cpu type"),
        ({"pids_limit": "1"}, "expected integer"),
        ({"init": 1}, "wrong value type"),
        ({"read_only": "true"}, "wrong value type"),
        ({"environment": {"x": 1}}, "mapping value type"),
        ({"labels": {"com.docker.compose.project": "foreign"}}, "reserved label"),
        ({"extra_hosts": ["invalid"]}, "extra host"),
        ({"extra_hosts": ["=127.0.0.1"]}, "extra host"),
        (
            {"env_file": [{"path": "/srv/o11y/data/secrets/token", "required": False}]},
            "optional env file",
        ),
        (
            {"env_file": [{"path": "/srv/o11y/data/secrets/token", "format": "raw"}]},
            "unknown object key",
        ),
        ({"volumes": [{"type": "image", "target": "/data"}]}, "mount type"),
        (
            {"volumes": [{"type": "volume", "source": "other", "target": "/data"}]},
            "foreign volume",
        ),
        (
            {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/srv/o11y/data/web",
                        "target": "/data",
                        "volume": {},
                    }
                ]
            },
            "mount option type",
        ),
        (
            {"volumes": [{"type": "tmpfs", "target": "/data", "source": "other"}]},
            "tmpfs source",
        ),
        (
            {
                "volumes": [
                    {"type": "bind", "source": "/srv/o11y/./data", "target": "/data"}
                ]
            },
            "path components",
        ),
        ({"networks": {"foreign": None}}, "unknown network"),
        ({"depends_on": {"foreign": {}}}, "unknown dependency"),
        ({"healthcheck": {"unknown": 1}}, "unknown object key"),
        ({"logging": {"driver": "syslog"}}, "logging driver"),
        (
            {"logging": {"driver": "json-file", "options": {"key": 1}}},
            "mapping value type",
        ),
        ({"ulimits": {"core": 10}}, "unknown object key"),
        ({"deploy": {"mode": "global"}}, "unknown object key"),
        ({"deploy": {"resources": {"limits": {"devices": []}}}}, "unknown object key"),
        ({"secrets": [{"source": "foreign"}]}, "unknown file reference"),
        (
            {
                "ports": [
                    {
                        "host_ip": "127.0.0.1",
                        "target": 80,
                        "published": "8081",
                        "protocol": "tcp",
                    }
                ]
            },
            "unlisted port",
        ),
        (
            {"ports": [{"target": 80, "published": "8080", "protocol": "tcp"}]},
            "expected string",
        ),
        (
            {
                "ports": [
                    {
                        "host_ip": "127.0.0.1",
                        "target": 80,
                        "published": "8080-8081",
                        "protocol": "tcp",
                    }
                ]
            },
            "published port",
        ),
    ],
)
def test_service_values_refuse(
    deploy_service: Service, changes: dict[str, Json], reason: str
) -> None:
    with pytest.raises(RefusalError) as caught:
        model.validate(minimal(**changes), deploy_service)
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"name": "foreign"}, "project name"),
        ({"services": {}}, "empty services"),
        ({"unknown": {}}, "top-level key"),
        ({"networks": {"default": {"name": "other_default"}}}, "physical name"),
        (
            {"networks": {"default": {"name": "o11y_default", "external": True}}},
            "unknown object key",
        ),
        (
            {"networks": {"default": {"name": "o11y_default", "driver": "host"}}},
            "network driver",
        ),
        (
            {
                "networks": {
                    "default": {"name": "o11y_default", "ipam": {"driver": "other"}}
                }
            },
            "unknown object key",
        ),
        (
            {
                "networks": {
                    "default": {
                        "name": "o11y_default",
                        "ipam": {"config": [{"subnet": "203.0.113.64/27"}]},
                    }
                }
            },
            "network subnet",
        ),
        (
            {"volumes": {"cache": {"name": "o11y_cache", "external": True}}},
            "unknown object key",
        ),
        (
            {"secrets": {"s": {"file": "/srv/o11y/file", "external": True}}},
            "unknown object key",
        ),
    ],
)
def test_top_values_refuse(
    deploy_service: Service, update: dict[str, Json], reason: str
) -> None:
    with pytest.raises(RefusalError) as caught:
        model.validate({**minimal(), **update}, deploy_service)
    assert str(caught.value) == reason


def test_network_options_independently(deploy_service: Service) -> None:
    value = minimal(networks={"default": {"aliases": ["web"]}})
    value["networks"] = {"default": {"name": "o11y_default"}}
    assert model.validate(value, deploy_service) == ()
    value = minimal(networks={"default": {"ipv4_address": "203.0.113.66"}})
    value["networks"] = {"default": {"name": "o11y_default"}}
    allowed = replace(deploy_service, networks={"default": "203.0.113.64/27"})
    assert model.validate(value, allowed) == ()


@pytest.mark.parametrize(
    "key",
    [
        "healthcheck",
        "logging",
        "deploy",
        "ulimits",
        "ports",
        "networks",
        "depends_on",
        "secrets",
        "configs",
    ],
)
def test_structured_unknown_keys(deploy_service: Service, key: str) -> None:
    value: Json = (
        [{"unknown": 1}] if key in {"ports", "secrets", "configs"} else {"unknown": 1}
    )
    with pytest.raises(RefusalError):
        model.validate(minimal(**{key: value}), deploy_service)


@pytest.mark.parametrize("target", [1, 65535])
def test_host_mode_and_target_port_boundaries(
    deploy_service: Service, target: int
) -> None:
    port: Json = {
        "mode": "host",
        "host_ip": "127.0.0.1",
        "target": target,
        "published": "8080",
        "protocol": "tcp",
    }
    assert model.validate(minimal(ports=[port]), deploy_service) == ()


def test_network_entries_after_an_empty_entry_are_checked(
    deploy_service: Service,
) -> None:
    candidate = minimal(
        networks={
            "empty": None,
            "default": {"ipv4_address": "203.0.113.66"},
        }
    )
    candidate["networks"] = {
        "empty": {"name": "o11y_empty"},
        "default": {"name": "o11y_default"},
    }
    with pytest.raises(RefusalError, match=r"^address outside subnet$"):
        model.validate(candidate, deploy_service)


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        ("=alias=127.0.0.1", "extra host"),
        (
            "name=alias=127.0.0.1",
            "'alias=127.0.0.1' does not appear to be an IPv4 or IPv6 address",
        ),
    ],
)
def test_extra_host_splits_only_the_first_separator(
    deploy_service: Service, host: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason) as caught:
        model.validate(minimal(extra_hosts=[host]), deploy_service)
    assert str(caught.value) == reason


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        ("registry.example/other@sha256:" + "a" * 64, "unlisted image repository"),
        ("registry.example/web:tag", "image digest"),
        ("web@sha256:" + "a" * 64, "image registry host"),
        ("example/web@sha256:" + "a" * 64, "image registry host"),
    ],
)
def test_image_allow_list_refuses(
    deploy_service: Service, image: str, reason: str
) -> None:
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        model.validate(minimal(image=image), deploy_service)


@pytest.mark.parametrize("repo", ["registry.example/web", "localhost:5000/team/web"])
@pytest.mark.parametrize("tag", ["", ":tag", ":Tag_1"])
def test_image_allow_list_accepts_digest_with_optional_tag(
    deploy_service: Service, repo: str, tag: str
) -> None:
    assert (
        model.validate(
            minimal(image=repo + tag + "@sha256:" + "a" * 64),
            replace(deploy_service, images=(repo,)),
        )
        == ()
    )


@pytest.mark.parametrize("kind", ["bind", "secrets", "configs"])
def test_read_only_sources_need_exact_permission(
    deploy_service: Service, kind: str
) -> None:
    path = Path("/srv/o11y/data/secrets/generated")
    if kind == "bind":
        candidate = minimal(
            volumes=[
                {
                    "type": "bind",
                    "source": str(path),
                    "target": "/secret",
                    "read_only": True,
                }
            ]
        )
    else:
        candidate = minimal()
        candidate[kind] = {"key": {"file": str(path)}}
    sources = model.validate(candidate, deploy_service)
    with pytest.raises(RefusalError, match=r"^unlisted read-only source$"):
        mounts.paths(sources, (), set(), deploy_service, RELEASE)
    mounts.paths(sources, (), set(), replace(deploy_service, ro_binds=(path,)), RELEASE)
    mounts.paths(
        sources,
        (),
        set(),
        replace(
            deploy_service, secrets=(replace(deploy_service.secrets[0], target=path),)
        ),
        RELEASE,
    )
    with pytest.raises(RefusalError, match=r"^unlisted read-only source$"):
        mounts.paths(
            sources,
            (),
            set(),
            replace(deploy_service, ro_binds=(path.parent,)),
            RELEASE,
        )


def test_o11y_golden_allow_lists(deploy_service: Service) -> None:
    fixture = json.loads(
        (Path(__file__).parents[2] / "testdata" / "deploy" / "o11y.json").read_text()
    )
    allowed = replace(
        deploy_service,
        images=tuple(fixture["images"]),
        ro_binds=tuple(Path(path) for path in fixture["ro_binds"]),
        secrets=(
            replace(
                deploy_service.secrets[0], target=Path("/srv/o11y/data/secrets/s3.env")
            ),
        ),
        ports=(
            ("127.0.0.1", 4317, "tcp"),
            ("127.0.0.1", 4318, "tcp"),
            ("127.0.0.1", 3000, "tcp"),
        ),
    )
    with pytest.raises(RefusalError, match=r"^runtime$"):
        model.validate(fixture["model"], allowed)
    for service in fixture["model"]["services"].values():
        service["runtime"] = "runsc"
    sources = model.validate(fixture["model"], allowed)
    mounts.paths(
        sources,
        (),
        {source.path for source in sources if source.writable},
        allowed,
        RELEASE,
    )


def test_storage_options_remain_outside_allow_list(deploy_service: Service) -> None:
    with pytest.raises(RefusalError, match=r"^unknown object key$"):
        model.validate(minimal(storage_opt={"size": "1g"}), deploy_service)


def test_service_requires_explicit_runtime(deploy_service: Service) -> None:
    candidate = minimal()
    mapping(mapping(candidate["services"])["web"]).pop("runtime", None)
    with pytest.raises(RefusalError, match=r"^runtime$"):
        model.validate(candidate, deploy_service)


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("services", "web", "ports", 0, "unknown"), True, "unknown object key"),
        (("services", "web", "ports", 0, "target"), 0, "integer below minimum"),
        (("services", "web", "ports", 0, "target"), 65536, "target port"),
        (
            ("services", "web", "networks", "default", "unknown"),
            True,
            "unknown object key",
        ),
        (
            ("services", "web", "networks", "default", "ipv4_address"),
            "203.0.113.66",
            "address outside subnet",
        ),
        (
            ("services", "web", "networks", "empty"),
            {"ipv4_address": "203.0.113.34"},
            "unlisted subnet",
        ),
        (
            ("services", "web", "depends_on", "web", "unknown"),
            True,
            "unknown object key",
        ),
        (("services", "web", "secrets", 0, "unknown"), True, "unknown object key"),
        (("networks", "default", "unknown"), True, "unknown object key"),
        (
            ("networks", "default", "labels", "com.docker.compose.project"),
            "foreign",
            "reserved label",
        ),
        (("volumes", "cache", "driver"), "bridge", "unknown object key"),
        (("name",), None, "project name"),
        (("services",), None, "expected an object"),
        (("services", "web"), None, "expected an object"),
        (("services", "web", "image"), None, "expected string"),
        (("services", "web", "extra_hosts"), None, "expected a string list"),
        (("services", "web", "extra_hosts", 0), None, "expected strings"),
        (("services", "web", "ports"), None, "expected table list"),
        (("services", "web", "ports", 0), None, "expected an object"),
        (("services", "web", "ports", 0, "mode"), None, "port mode"),
        (("services", "web", "ports", 0, "mode"), [], "port mode"),
        (("services", "web", "ports", 0, "mode"), {}, "port mode"),
        (("services", "web", "ports", 0, "host_ip"), None, "expected string"),
        (("services", "web", "ports", 0, "target"), None, "expected integer"),
        (("services", "web", "ports", 0, "published"), None, "expected string"),
        (("services", "web", "ports", 0, "protocol"), None, "expected string"),
        (("services", "web", "networks"), None, "expected an object"),
        (("services", "web", "networks", "default"), "wrong", "expected an object"),
        (
            ("services", "web", "networks", "default", "aliases"),
            None,
            "expected a string list",
        ),
        (
            ("services", "web", "networks", "default", "aliases", 0),
            None,
            "expected strings",
        ),
        (
            ("services", "web", "networks", "default", "ipv4_address"),
            None,
            "expected string",
        ),
        (("services", "web", "networks", "empty"), "wrong", "expected an object"),
        (("services", "web", "depends_on"), None, "expected an object"),
        (("services", "web", "depends_on", "web"), None, "expected an object"),
        (
            ("services", "web", "depends_on", "web", "condition"),
            None,
            "expected string",
        ),
        (
            ("services", "web", "depends_on", "web", "required"),
            None,
            "wrong value type",
        ),
        (("services", "web", "depends_on", "web", "restart"), None, "wrong value type"),
        (("services", "web", "secrets"), None, "expected table list"),
        (("services", "web", "secrets", 0), None, "expected an object"),
        (("services", "web", "secrets", 0, "source"), None, "expected string"),
        (("services", "web", "secrets", 0, "target"), None, "expected string"),
        (("services", "web", "secrets", 0, "uid"), None, "expected string"),
        (("services", "web", "secrets", 0, "gid"), None, "expected string"),
        (("services", "web", "secrets", 0, "mode"), None, "expected integer"),
        (("services", "web", "secrets", 0, "mode"), [], "expected integer"),
        (("services", "web", "secrets", 0, "mode"), {}, "expected integer"),
        (("services", "web", "configs"), None, "expected table list"),
        (("services", "web", "configs", 0), None, "expected an object"),
        (("services", "web", "configs", 0, "source"), None, "expected string"),
        (("networks",), None, "expected an object"),
        (("networks", "default"), None, "expected an object"),
        (("networks", "default", "name"), None, "physical name"),
        (("networks", "default", "driver"), 1, "network driver"),
        (("networks", "default", "driver"), [], "network driver"),
        (("networks", "default", "driver"), {}, "network driver"),
        (("networks", "default", "internal"), None, "wrong value type"),
        (("networks", "default", "labels"), None, "expected an object"),
        (("networks", "default", "labels", "owner"), None, "mapping value type"),
        (("networks", "default", "ipam"), None, "expected an object"),
        (("networks", "default", "ipam", "config"), None, "network subnet"),
        (("networks", "default", "ipam", "config", 0), None, "network subnet"),
        (
            ("networks", "default", "ipam", "config", 0, "subnet"),
            None,
            "network subnet",
        ),
        (("networks", "empty"), None, "expected an object"),
        (("networks", "empty", "name"), None, "physical name"),
        (("networks", "empty", "ipam"), None, "expected an object"),
        (("volumes",), None, "expected an object"),
        (("volumes", "cache"), None, "expected an object"),
        (("volumes", "cache", "name"), None, "physical name"),
        (("volumes", "cache", "labels"), None, "expected an object"),
    ],
)
def test_nested_model_types(
    deploy_service: Service, path: tuple[str | int, ...], value: Json, reason: str
) -> None:
    with pytest.raises((RefusalError, ValueError)) as caught:
        model.validate(changed_kitchen(path, value), deploy_service)
    assert str(caught.value) == reason
