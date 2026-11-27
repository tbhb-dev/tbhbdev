"""Shared minimal Compose model for deployment policy and engine tests."""

lazy from tbhbops.deploy.protocol import Json, array, mapping

RELEASE = "a" * 40
IMAGE = "registry.example/web@sha256:" + "a" * 64


def minimal(**changes: Json) -> dict[str, Json]:
    result: dict[str, Json] = {
        "name": "o11y",
        "services": {
            "web": {
                "image": IMAGE,
                "runtime": "runsc",
                "mem_limit": "1024",
                "pids_limit": 100,
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
                **changes,
            }
        },
    }
    return result


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


def changed_kitchen(path: tuple[str | int, ...], value: Json) -> dict[str, Json]:
    candidate = kitchen()
    parent: Json = candidate
    for key in path[:-1]:
        parent = mapping(parent)[key] if isinstance(key, str) else array(parent)[key]
    last = path[-1]
    if isinstance(last, str):
        mapping(parent)[last] = value
    else:
        array(parent)[last] = value
    return candidate
