"""Policy checks the normalized model, including every nested key boundary."""

# gazelle:include_dep :tests
import json
from dataclasses import replace
from pathlib import Path

import pytest
from tbhbops.deploy import policy
from tbhbops.deploy.protocol import Json, RefusalError, array, mapping
lazy from tbhbops.deploy.config import Service

from .deploy_model import IMAGE, minimal
from .test_deploy_config import deploy_service as deploy_service  # noqa: PLC0414  # pytest fixture registration

RELEASE = "a" * 40


def test_normalized_minimal(deploy_service: Service) -> None:
    assert policy.model(minimal(command=None, entrypoint=None), deploy_service) == ()
    assert (
        policy.model(minimal(command=["true"], entrypoint=["sh"]), deploy_service) == ()
    )
    assert (
        policy.model({**minimal(), "x-inert": {"privileged": True}}, deploy_service)
        == ()
    )


def kitchen() -> dict[str, Json]:
    return {
        "name": "o11y",
        "services": {
            "web": {
                "image": IMAGE,
                "command": ["true"],
                "entrypoint": None,
                "environment": {"VAR": "text", "EMPTY": None},
                "labels": {"owner": "ops"},
                "env_file": [
                    {"path": "/srv/o11y/data/secrets/token", "required": True}
                ],
                "user": "1:1",
                "working_dir": "/app",
                "restart": "always",
                "runtime": "runsc",
                "init": True,
                "read_only": True,
                "cpus": 0.5,
                "pids_limit": 100,
                "mem_limit": "805306368",
                "memswap_limit": "805306368",
                "stop_grace_period": "10s",
                "stop_signal": "SIGTERM",
                "tmpfs": ["/scratch"],
                "expose": ["8080"],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "extra_hosts": ["example=127.0.0.1"],
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/srv/o11y/data/web",
                        "target": "/data",
                        "read_only": False,
                        "bind": {"create_host_path": False},
                    },
                    {
                        "type": "bind",
                        "source": f"/srv/o11y/releases/{RELEASE}/config",
                        "target": "/etc/web",
                        "read_only": True,
                        "bind": {},
                    },
                    {
                        "type": "volume",
                        "source": "cache",
                        "target": "/cache",
                        "volume": {"nocopy": True},
                    },
                    {
                        "type": "tmpfs",
                        "target": "/scratch",
                        "tmpfs": {"size": 1024, "mode": 448},
                    },
                ],
                "ports": [
                    {
                        "mode": "ingress",
                        "host_ip": "127.0.0.1",
                        "target": 80,
                        "published": "8080",
                        "protocol": "tcp",
                    }
                ],
                "networks": {
                    "default": {"aliases": ["web"], "ipv4_address": "203.0.113.34"},
                    "empty": None,
                },
                "depends_on": {
                    "web": {
                        "condition": "service_started",
                        "required": True,
                        "restart": False,
                    }
                },
                "healthcheck": {
                    "test": ["CMD", "true"],
                    "interval": "1s",
                    "timeout": "1s",
                    "retries": 3,
                    "start_period": "1s",
                    "start_interval": "1s",
                    "disable": False,
                },
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "1m", "max-file": "3"},
                },
                "deploy": {
                    "resources": {
                        "limits": {"cpus": "1", "memory": "1024", "pids": 10},
                        "reservations": {"cpus": "0.5", "memory": "512"},
                    }
                },
                "ulimits": {"nofile": {"soft": 10, "hard": 20}, "nproc": 10},
                "secrets": [
                    {
                        "source": "token",
                        "target": "token",
                        "uid": "1",
                        "gid": "1",
                        "mode": 256,
                    }
                ],
                "configs": [{"source": "config"}],
            }
        },
        "networks": {
            "default": {
                "name": "o11y_default",
                "driver": "bridge",
                "internal": False,
                "labels": {"owner": "ops"},
                "ipam": {"config": [{"subnet": "203.0.113.32/27"}]},
            },
            "empty": {"name": "o11y_empty", "ipam": {}},
        },
        "volumes": {"cache": {"name": "o11y_cache", "labels": {}}},
        "secrets": {"token": {"name": "token", "file": "/srv/o11y/data/secrets/token"}},
        "configs": {"config": {"file": f"/srv/o11y/releases/{RELEASE}/config/web"}},
    }


def test_all_normalized_forms(deploy_service: Service) -> None:
    sources = policy.model(kitchen(), deploy_service)
    assert len(sources) == 5
    assert sum(source.writable for source in sources) == 1
    writable = {source.path for source in sources if source.writable}
    policy.paths(sources, (), writable, deploy_service, RELEASE)
    assert (
        policy.model(
            minimal(runtime="runc"), replace(deploy_service, runc_exceptions=("web",))
        )
        == ()
    )
    assert policy.model(minimal(cpus=1), deploy_service) == ()


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
        policy.model(minimal(**{key: True}), deploy_service)
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
        policy.model(minimal(**changes), deploy_service)
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
        policy.model({**minimal(), **update}, deploy_service)
    assert str(caught.value) == reason


def test_cross_release_writable_set(deploy_service: Service) -> None:
    writable = policy.Source(Path("/srv/o11y/data/web"), writable=True)
    nested = policy.Source(writable.path / "config")
    with pytest.raises(RefusalError, match=r"^path under writable bind$"):
        policy.paths((nested,), (writable,), {writable.path}, deploy_service, RELEASE)
    with pytest.raises(RefusalError, match=r"^writable retained path$"):
        policy.paths((writable,), (nested,), {writable.path}, deploy_service, RELEASE)
    policy.paths((writable,), (writable,), {writable.path}, deploy_service, RELEASE)
    with pytest.raises(RefusalError, match=r"^incomplete writable set$"):
        policy.paths((writable,), (), set(), deploy_service, RELEASE)


def test_network_options_independently(deploy_service: Service) -> None:
    value = minimal(networks={"default": {"aliases": ["web"]}})
    value["networks"] = {"default": {"name": "o11y_default"}}
    assert policy.model(value, deploy_service) == ()
    value = minimal(networks={"default": {"ipv4_address": "203.0.113.66"}})
    value["networks"] = {"default": {"name": "o11y_default"}}
    allowed = replace(deploy_service, networks={"default": "203.0.113.64/27"})
    assert policy.model(value, allowed) == ()


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("services", "web", "volumes", 0, "unknown"), True, "unknown object key"),
        (
            ("services", "web", "volumes", 0, "bind", "unknown"),
            True,
            "unknown object key",
        ),
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
        (
            ("services", "web", "deploy", "resources", "unknown"),
            {},
            "unknown object key",
        ),
        (
            ("services", "web", "deploy", "resources", "reservations", "pids"),
            1,
            "unknown object key",
        ),
        (("services", "web", "ulimits", "nofile", "unknown"), 1, "unknown object key"),
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
        (("services", "web", "command"), "wrong", "expected a string list"),
        (("services", "web", "command", 0), None, "expected strings"),
        (("services", "web", "entrypoint"), "wrong", "expected a string list"),
        (("services", "web", "environment"), None, "expected an object"),
        (("services", "web", "environment", "VAR"), 1, "mapping value type"),
        (("services", "web", "environment", "EMPTY"), 1, "mapping value type"),
        (("services", "web", "labels"), None, "expected an object"),
        (("services", "web", "labels", "owner"), None, "mapping value type"),
        (("services", "web", "env_file"), None, "expected table list"),
        (("services", "web", "env_file", 0), None, "expected an object"),
        (("services", "web", "env_file", 0, "path"), None, "expected string"),
        (("services", "web", "env_file", 0, "required"), None, "optional env file"),
        (("services", "web", "user"), None, "expected string"),
        (("services", "web", "working_dir"), None, "expected string"),
        (("services", "web", "restart"), None, "expected string"),
        (("services", "web", "runtime"), None, "expected string"),
        (("services", "web", "runtime"), [], "expected string"),
        (("services", "web", "runtime"), {}, "expected string"),
        (("services", "web", "init"), None, "wrong value type"),
        (("services", "web", "read_only"), None, "wrong value type"),
        (("services", "web", "cpus"), None, "cpu type"),
        (("services", "web", "pids_limit"), None, "expected integer"),
        (("services", "web", "mem_limit"), None, "expected string"),
        (("services", "web", "memswap_limit"), None, "expected string"),
        (("services", "web", "stop_grace_period"), None, "expected string"),
        (("services", "web", "stop_signal"), None, "expected string"),
        (("services", "web", "tmpfs"), None, "expected a string list"),
        (("services", "web", "tmpfs", 0), None, "expected strings"),
        (("services", "web", "expose"), None, "expected a string list"),
        (("services", "web", "expose", 0), None, "expected strings"),
        (("services", "web", "cap_drop"), None, "expected a string list"),
        (("services", "web", "cap_drop", 0), None, "expected strings"),
        (("services", "web", "security_opt"), None, "expected a string list"),
        (("services", "web", "security_opt", 0), None, "expected strings"),
        (("services", "web", "extra_hosts"), None, "expected a string list"),
        (("services", "web", "extra_hosts", 0), None, "expected strings"),
        (("services", "web", "volumes"), None, "expected table list"),
        (("services", "web", "volumes", 0), None, "expected an object"),
        (("services", "web", "volumes", 0, "type"), None, "expected string"),
        (("services", "web", "volumes", 0, "source"), None, "expected string"),
        (("services", "web", "volumes", 0, "target"), None, "expected string"),
        (("services", "web", "volumes", 0, "read_only"), None, "wrong value type"),
        (("services", "web", "volumes", 0, "bind"), None, "expected an object"),
        (
            ("services", "web", "volumes", 0, "bind", "create_host_path"),
            None,
            "wrong value type",
        ),
        (("services", "web", "volumes", 1), None, "expected an object"),
        (("services", "web", "volumes", 1, "type"), None, "expected string"),
        (("services", "web", "volumes", 1, "source"), None, "expected string"),
        (("services", "web", "volumes", 1, "target"), None, "expected string"),
        (("services", "web", "volumes", 1, "read_only"), None, "wrong value type"),
        (("services", "web", "volumes", 1, "bind"), None, "expected an object"),
        (("services", "web", "volumes", 2), None, "expected an object"),
        (("services", "web", "volumes", 2, "type"), None, "expected string"),
        (("services", "web", "volumes", 2, "source"), None, "expected string"),
        (("services", "web", "volumes", 2, "target"), None, "expected string"),
        (("services", "web", "volumes", 2, "volume"), None, "expected an object"),
        (
            ("services", "web", "volumes", 2, "volume", "nocopy"),
            None,
            "wrong value type",
        ),
        (("services", "web", "volumes", 3), None, "expected an object"),
        (("services", "web", "volumes", 3, "type"), None, "expected string"),
        (("services", "web", "volumes", 3, "target"), None, "expected string"),
        (("services", "web", "volumes", 3, "tmpfs"), None, "expected an object"),
        (("services", "web", "volumes", 3, "tmpfs", "size"), None, "wrong value type"),
        (("services", "web", "volumes", 3, "tmpfs", "mode"), None, "wrong value type"),
        (("services", "web", "volumes", 3, "tmpfs", "mode"), [], "wrong value type"),
        (("services", "web", "volumes", 3, "tmpfs", "mode"), {}, "wrong value type"),
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
        (("services", "web", "healthcheck"), None, "expected an object"),
        (("services", "web", "healthcheck", "test"), None, "expected a string list"),
        (("services", "web", "healthcheck", "test", 0), None, "expected strings"),
        (("services", "web", "healthcheck", "test", 1), None, "expected strings"),
        (("services", "web", "healthcheck", "interval"), None, "expected string"),
        (("services", "web", "healthcheck", "timeout"), None, "expected string"),
        (("services", "web", "healthcheck", "retries"), None, "expected integer"),
        (("services", "web", "healthcheck", "start_period"), None, "expected string"),
        (("services", "web", "healthcheck", "start_interval"), None, "expected string"),
        (("services", "web", "healthcheck", "disable"), None, "wrong value type"),
        (("services", "web", "logging"), None, "expected an object"),
        (("services", "web", "logging", "driver"), 1, "logging driver"),
        (("services", "web", "logging", "driver"), [], "logging driver"),
        (("services", "web", "logging", "driver"), {}, "logging driver"),
        (("services", "web", "logging", "options"), None, "expected an object"),
        (
            ("services", "web", "logging", "options", "max-size"),
            None,
            "mapping value type",
        ),
        (("services", "web", "deploy"), None, "expected an object"),
        (("services", "web", "deploy", "resources"), None, "expected an object"),
        (
            ("services", "web", "deploy", "resources", "limits"),
            None,
            "expected an object",
        ),
        (
            ("services", "web", "deploy", "resources", "limits", "cpus"),
            None,
            "expected string",
        ),
        (
            ("services", "web", "deploy", "resources", "limits", "memory"),
            None,
            "expected string",
        ),
        (
            ("services", "web", "deploy", "resources", "limits", "pids"),
            None,
            "expected integer",
        ),
        (
            ("services", "web", "deploy", "resources", "reservations"),
            None,
            "expected an object",
        ),
        (
            ("services", "web", "deploy", "resources", "reservations", "cpus"),
            None,
            "expected string",
        ),
        (
            ("services", "web", "deploy", "resources", "reservations", "memory"),
            None,
            "expected string",
        ),
        (("services", "web", "ulimits"), None, "expected an object"),
        (("services", "web", "ulimits", "nofile"), None, "expected an object"),
        (("services", "web", "ulimits", "nofile", "soft"), None, "wrong value type"),
        (("services", "web", "ulimits", "nofile", "hard"), None, "wrong value type"),
        (("services", "web", "ulimits", "nproc"), None, "expected an object"),
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
        (("secrets",), None, "expected an object"),
        (("secrets", "token"), None, "expected an object"),
        (("secrets", "token", "name"), None, "expected string"),
        (("secrets", "token", "file"), None, "expected string"),
        (("configs",), None, "expected an object"),
        (("configs", "config"), None, "expected an object"),
        (("configs", "config", "file"), None, "expected string"),
    ],
)
def test_nested_schema_types(
    deploy_service: Service, path: tuple[str | int, ...], value: Json, reason: str
) -> None:
    model = kitchen()
    parent: Json = model
    for key in path[:-1]:
        parent = mapping(parent)[key] if isinstance(key, str) else array(parent)[key]
    last = path[-1]
    if isinstance(last, str):
        mapping(parent)[last] = value
    else:
        array(parent)[last] = value
    with pytest.raises((RefusalError, ValueError)) as caught:
        policy.model(model, deploy_service)
    assert str(caught.value) == reason


def test_exact_model_sources(deploy_service: Service) -> None:
    assert policy.model(kitchen(), deploy_service) == (
        policy.Source(Path("/srv/o11y/data/secrets/token"), file=True, env=True),
        policy.Source(Path("/srv/o11y/data/web"), writable=True),
        policy.Source(Path(f"/srv/o11y/releases/{RELEASE}/config")),
        policy.Source(Path("/srv/o11y/data/secrets/token"), file=True),
        policy.Source(Path(f"/srv/o11y/releases/{RELEASE}/config/web"), file=True),
    )


@pytest.mark.parametrize(
    ("path", "write", "env", "reason"),
    [
        ("/outside", False, False, "path outside root"),
        ("/srv/o11y/data/a b", False, False, "path characters"),
        ("/srv/o11y/../outside", False, False, "path outside root"),
        ("/srv/o11y", False, False, "path outside root"),
        ("/srv/o11y/deploy/model", False, False, "read-only control path"),
        ("/srv/o11y/incoming/model", False, False, "read-only control path"),
        ("/srv/o11y/releases/other/model", False, False, "foreign release path"),
        ("/srv/o11y/data/file", False, True, "unlisted env file"),
        ("/srv/o11y/deploy", True, False, "writable control path"),
        ("/srv/o11y/deploy/subdir", True, False, "writable control path"),
        ("/srv/o11y/incoming", True, False, "writable control path"),
        ("/srv/o11y/releases", True, False, "writable control path"),
        ("/srv/o11y/data", True, False, "writable control path"),
        ("/srv/o11y/data/secrets", True, False, "writable control path"),
        ("/srv/o11y/data/secrets/token", True, False, "writable control path"),
    ],
)
def test_path_refusal_reasons(
    deploy_service: Service, path: str, write: bool, env: bool, reason: str
) -> None:
    source = policy.Source(Path(path), writable=write, env=env)
    with pytest.raises(RefusalError) as caught:
        policy.paths(
            (source,), (), {source.path} if write else set(), deploy_service, RELEASE
        )
    assert str(caught.value) == reason


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
        policy.model(minimal(**{key: value}), deploy_service)


def test_string_map_defaults_do_not_treat_options_as_labels() -> None:
    policy.string_map({"com.docker.compose.project": "value"})
    with pytest.raises(RefusalError, match=r"^mapping value type$"):
        policy.string_map({"key": None})


def test_valid_empty_resource_and_logging_defaults(deploy_service: Service) -> None:
    assert policy.model(minimal(deploy={}), deploy_service) == ()
    with pytest.raises(RefusalError, match=r"^logging driver$"):
        policy.model(minimal(logging={"driver": "journald"}), deploy_service)


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
    assert policy.model(minimal(ports=[port]), deploy_service) == ()


def test_bind_without_read_only_is_writable(deploy_service: Service) -> None:
    candidate = minimal(
        volumes=[
            {
                "type": "bind",
                "source": "/srv/o11y/data/web",
                "target": "/data",
            }
        ]
    )
    assert policy.model(candidate, deploy_service) == (
        policy.Source(Path("/srv/o11y/data/web"), writable=True),
    )


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
        policy.model(candidate, deploy_service)


def test_ulimits_after_a_numeric_entry_are_checked(deploy_service: Service) -> None:
    with pytest.raises(RefusalError, match=r"^wrong value type$"):
        policy.model(
            minimal(ulimits={"nofile": 10, "nproc": {"soft": False}}), deploy_service
        )


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
        policy.model(minimal(extra_hosts=[host]), deploy_service)
    assert str(caught.value) == reason


def test_read_only_bind_cannot_alias_a_writable_bind(deploy_service: Service) -> None:
    path = Path("/srv/o11y/data/web")
    with pytest.raises(RefusalError, match=r"^path under writable bind$"):
        policy.paths((policy.Source(path),), (), {path}, deploy_service, RELEASE)


@pytest.mark.parametrize(
    "old",
    [
        policy.Source(Path("/srv/o11y/data/web")),
        policy.Source(Path("/srv/o11y/data/web/child"), writable=True),
    ],
)
def test_writable_bind_preserves_retained_readers_and_children(
    deploy_service: Service, old: policy.Source
) -> None:
    source = policy.Source(Path("/srv/o11y/data/web"), writable=True)
    with pytest.raises(RefusalError, match=r"^writable retained path$"):
        policy.paths((source,), (old,), {source.path}, deploy_service, RELEASE)


def test_release_local_env_file_is_allowed(deploy_service: Service) -> None:
    source = policy.Source(Path(f"/srv/o11y/releases/{RELEASE}/compose.env"), env=True)
    policy.paths((source,), (), set(), deploy_service, RELEASE)


def test_ordinary_uppercase_and_dotted_components_are_allowed(
    deploy_service: Service,
) -> None:
    source = policy.Source(Path("/srv/o11y/data/XX.XX"))
    policy.paths(
        (source,), (), set(), replace(deploy_service, ro_binds=(source.path,)), RELEASE
    )


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
        policy.model(minimal(image=image), deploy_service)


@pytest.mark.parametrize("repo", ["registry.example/web", "localhost:5000/team/web"])
@pytest.mark.parametrize("tag", ["", ":tag", ":Tag_1"])
def test_image_allow_list_accepts_digest_with_optional_tag(
    deploy_service: Service, repo: str, tag: str
) -> None:
    assert (
        policy.model(
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
    sources = policy.model(candidate, deploy_service)
    with pytest.raises(RefusalError, match=r"^unlisted read-only source$"):
        policy.paths(sources, (), set(), deploy_service, RELEASE)
    policy.paths(sources, (), set(), replace(deploy_service, ro_binds=(path,)), RELEASE)
    policy.paths(
        sources,
        (),
        set(),
        replace(
            deploy_service, secrets=(replace(deploy_service.secrets[0], target=path),)
        ),
        RELEASE,
    )
    with pytest.raises(RefusalError, match=r"^unlisted read-only source$"):
        policy.paths(
            sources,
            (),
            set(),
            replace(deploy_service, ro_binds=(path.parent,)),
            RELEASE,
        )


@pytest.mark.parametrize(
    "path",
    [
        "/srv/o11y/deploy/model",
        "/srv/o11y/incoming/file",
        "/srv/o11y/releases/other/file",
    ],
)
def test_allow_list_never_overrides_control_paths(
    deploy_service: Service, path: str
) -> None:
    source = policy.Source(Path(path))
    with pytest.raises(RefusalError):
        policy.paths(
            (source,),
            (),
            set(),
            replace(deploy_service, ro_binds=(source.path,)),
            RELEASE,
        )


def test_read_only_permission_does_not_allow_env_file(deploy_service: Service) -> None:
    path = Path("/srv/o11y/data/settings.env")
    with pytest.raises(RefusalError, match=r"^unlisted env file$"):
        policy.paths(
            (policy.Source(path, file=True, env=True),),
            (),
            set(),
            replace(deploy_service, ro_binds=(path,)),
            RELEASE,
        )


def test_o11y_golden_allow_lists(deploy_service: Service) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "testdata" / "deploy" / "o11y.json").read_text()
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
        policy.model(fixture["model"], allowed)
    for service in fixture["model"]["services"].values():
        service["runtime"] = "runsc"
    sources = policy.model(fixture["model"], allowed)
    policy.paths(
        sources,
        (),
        {source.path for source in sources if source.writable},
        allowed,
        RELEASE,
    )


@pytest.mark.parametrize(
    "duration",
    [
        "0s",
        "60s",
        "1m",
        "1m0s",
        "59.5s500ms",
        "60000ms",
        "60000000us",
        "60000000000ns",
        "0.01h",
    ],
)
def test_stop_grace_bounded_durations(deploy_service: Service, duration: str) -> None:
    assert policy.model(minimal(stop_grace_period=duration), deploy_service) == ()


@pytest.mark.parametrize(
    "duration", ["60.001s", "1m1s", "1h", "60001ms", "60000001us", "60000000001ns"]
)
def test_stop_grace_ceiling(deploy_service: Service, duration: str) -> None:
    with pytest.raises(RefusalError, match=r"^stop grace ceiling$"):
        policy.model(minimal(stop_grace_period=duration), deploy_service)


@pytest.mark.parametrize("duration", ["", "forever", "61", "-1s", "1s-junk", "NaNs"])
def test_stop_grace_unparseable(deploy_service: Service, duration: str) -> None:
    with pytest.raises(RefusalError, match=r"^stop grace duration$"):
        policy.model(minimal(stop_grace_period=duration), deploy_service)


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("mem_limit", "memory limit required"),
        ("pids_limit", "expected integer"),
        ("logging", "expected an object"),
    ],
)
def test_every_service_requires_limits(
    deploy_service: Service, key: str, reason: str
) -> None:
    value = minimal()
    services = mapping(value["services"])
    services["unbounded"] = dict(mapping(services["web"]))
    del mapping(services["unbounded"])[key]
    with pytest.raises(RefusalError) as caught:
        policy.model(value, deploy_service)
    assert str(caught.value) == reason


@pytest.mark.parametrize("memory", ["0", "0m", "-1", "unlimited", "", "1.0"])
def test_memory_limit_must_be_positive(deploy_service: Service, memory: str) -> None:
    with pytest.raises(RefusalError, match=r"^positive size required$"):
        policy.model(minimal(mem_limit=memory), deploy_service)


@pytest.mark.parametrize("pids", [0, -1])
def test_pid_limit_must_be_positive(deploy_service: Service, pids: int) -> None:
    with pytest.raises(RefusalError, match=r"^integer below minimum$"):
        policy.model(minimal(pids_limit=pids), deploy_service)


def test_pid_limit_accepts_one_process(deploy_service: Service) -> None:
    assert policy.model(minimal(pids_limit=1), deploy_service) == ()


@pytest.mark.parametrize("memory", ["1", "1024", "001024", "1K", "1g", "2M", "1b"])
def test_memory_limit_alternate_spelling(deploy_service: Service, memory: str) -> None:
    value = minimal(deploy={"resources": {"limits": {"memory": memory}}})
    del mapping(mapping(value["services"])["web"])["mem_limit"]
    assert policy.model(value, deploy_service) == ()


@pytest.mark.parametrize("key", ["max-size", "max-file"])
@pytest.mark.parametrize("value", [None, "0", "-1", "unlimited", ""])
def test_logging_requires_rotation(
    deploy_service: Service, key: str, value: Json
) -> None:
    logging: dict[str, Json] = {
        "driver": "json-file",
        "options": {"max-size": "10m", "max-file": "3"},
    }
    options = mapping(logging["options"])
    if value is None:
        del options[key]
    else:
        options[key] = value
    with pytest.raises(RefusalError) as caught:
        policy.model(minimal(logging=logging), deploy_service)
    expected = (
        "expected string"
        if value is None
        else "positive size required"
        if key == "max-size"
        else "positive log file count required"
    )
    assert str(caught.value) == expected


def test_logging_rejects_unknown_keys(deploy_service: Service) -> None:
    logging = mapping(mapping(mapping(minimal()["services"])["web"])["logging"])
    logging["unknown"] = True
    with pytest.raises(RefusalError, match=r"^unknown object key$"):
        policy.model(minimal(logging=logging), deploy_service)


def test_logging_without_options_reports_missing_rotation(
    deploy_service: Service,
) -> None:
    with pytest.raises(RefusalError, match=r"^expected string$"):
        policy.model(minimal(logging={"driver": "json-file"}), deploy_service)


@pytest.mark.parametrize("primary", ["0", "1024"])
def test_conflicting_memory_limits_cannot_enable_unlimited(
    deploy_service: Service, primary: str
) -> None:
    with pytest.raises(RefusalError, match=r"^positive size required$"):
        policy.model(
            minimal(
                mem_limit=primary, deploy={"resources": {"limits": {"memory": "0"}}}
            ),
            deploy_service,
        )


def test_storage_options_remain_outside_allow_list(deploy_service: Service) -> None:
    with pytest.raises(RefusalError, match=r"^unknown object key$"):
        policy.model(minimal(storage_opt={"size": "1g"}), deploy_service)


@pytest.mark.parametrize(
    "source", ["data/config/file", "data/config/file/child", "data/config", "data"]
)
def test_writable_bind_cannot_overlap_read_only_control(
    deploy_service: Service, source: str
) -> None:
    allowed = replace(deploy_service, ro_binds=(Path("/srv/o11y/data/config/file"),))
    path = allowed.root / source
    with pytest.raises(RefusalError, match=r"^writable control path$"):
        policy.paths(
            (policy.Source(path, writable=True),), (), {path}, allowed, RELEASE
        )


def test_writable_bind_cannot_occupy_read_only_control_directory(
    deploy_service: Service,
) -> None:
    allowed = replace(deploy_service, ro_binds=(Path("/srv/o11y/data/config/file"),))
    path = allowed.root / "data/config/sibling"
    with pytest.raises(RefusalError, match=r"^writable control path$"):
        policy.paths(
            (policy.Source(path, writable=True),), (), {path}, allowed, RELEASE
        )


@pytest.mark.parametrize(
    "key",
    [
        "env",
        "env-regex",
        "labels-regex",
        "ENV",
        "Env-Regex",
        "LABELS-REGEX",
        "env_regex",
        "labels_regex",
    ],
)
def test_logging_refuses_secret_attribute_options(
    deploy_service: Service, key: str
) -> None:
    logging: Json = {
        "driver": "json-file",
        "options": {"max-size": "10m", "max-file": "3", key: ".*"},
    }
    with pytest.raises(RefusalError, match=r"^unsafe logging option$"):
        policy.model(
            minimal(
                logging=logging,
                env_file=[{"path": "/srv/o11y/data/secrets/token", "required": True}],
            ),
            deploy_service,
        )


@pytest.mark.parametrize(
    "options", [{"compress": "true"}, {"labels": "owner"}, {"tag": "{{.Name}}"}]
)
def test_logging_allows_harmless_options(
    deploy_service: Service, options: dict[str, Json]
) -> None:
    assert (
        policy.model(
            minimal(
                logging={
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3", **options},
                }
            ),
            deploy_service,
        )
        == ()
    )


@pytest.mark.parametrize("value", ["-1", "0", "unlimited", "arbitrary", "", "1.5g"])
def test_memswap_limit_requires_positive_size(
    deploy_service: Service, value: str
) -> None:
    with pytest.raises(RefusalError, match=r"^positive size required$"):
        policy.model(minimal(memswap_limit=value), deploy_service)


@pytest.mark.parametrize("value", ["1", "001024", "1b", "2M", "1g"])
def test_memswap_limit_accepts_memory_sizes(
    deploy_service: Service, value: str
) -> None:
    assert policy.model(minimal(memswap_limit=value), deploy_service) == ()


def test_retained_reads_only_host_sources() -> None:
    value: Json = {
        "services": {
            "old": {
                "image": "retired",
                "runtime": "retired",
                "env_file": [{"path": "/srv/env"}],
                "volumes": [
                    {"type": "bind", "source": "/srv/rw"},
                    {"type": "bind", "source": "/srv/ro", "read_only": True},
                    {"type": "volume", "source": "named"},
                ],
            },
            "empty": {},
        },
        "secrets": {"s": {"file": "/srv/secret"}},
        "configs": {"c": {"file": "/srv/config"}},
    }
    assert policy.retained(value) == (
        policy.Source(Path("/srv/env"), file=True, env=True),
        policy.Source(Path("/srv/rw"), writable=True),
        policy.Source(Path("/srv/ro")),
        policy.Source(Path("/srv/secret"), file=True),
        policy.Source(Path("/srv/config"), file=True),
    )
    assert policy.retained({"services": {"empty": {}}}) == ()


def test_service_requires_explicit_runtime(deploy_service: Service) -> None:
    candidate = minimal()
    mapping(mapping(candidate["services"])["web"]).pop("runtime", None)
    with pytest.raises(RefusalError, match=r"^runtime$"):
        policy.model(candidate, deploy_service)


@pytest.mark.parametrize("suffix", ["", "b", "B", "k", "K", "m", "M", "g", "G"])
@pytest.mark.parametrize("key", ["mem_limit", "memswap_limit", "deploy", "logging"])
def test_resource_size_ceiling_boundaries(
    deploy_service: Service, suffix: str, key: str
) -> None:
    ceiling = 100 * 1024**2 if key == "logging" else 64 * 1024**3
    multiplier = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}.get(suffix.lower(), 1)
    # A log size in whole GiB already exceeds the ceiling at its first value.
    if ceiling < multiplier:
        return
    for delta in (0, 1):
        size = str(ceiling // multiplier + delta) + suffix
        value: Json = size
        if key == "deploy":
            value = {"resources": {"limits": {"memory": size}}}
        elif key == "logging":
            value = {
                "driver": "json-file",
                "options": {"max-size": size, "max-file": "3"},
            }
        candidate = minimal(**{key: value})
        if delta:
            with pytest.raises(RefusalError, match=r"^size ceiling$"):
                policy.model(candidate, deploy_service)
        else:
            assert policy.model(candidate, deploy_service) == ()
