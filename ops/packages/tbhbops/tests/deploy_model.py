"""Shared minimal Compose model for deployment policy and engine tests."""

lazy from tbhbops.deploy.protocol import Json

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
