"""Validate Compose mount sources and host path restrictions."""

from dataclasses import replace
from pathlib import Path

import pytest
from tbhbops.deploy import model, mounts
from tbhbops.deploy.protocol import Json, RefusalError
lazy from tbhbops.deploy.config import Service

from ...deploy_model import RELEASE, changed_kitchen, kitchen, minimal


def test_cross_release_writable_set(deploy_service: Service) -> None:
    writable = mounts.Source(Path("/srv/o11y/data/web"), writable=True)
    nested = mounts.Source(writable.path / "config")
    with pytest.raises(RefusalError, match=r"^path under writable bind$"):
        mounts.paths((nested,), (writable,), {writable.path}, deploy_service, RELEASE)
    with pytest.raises(RefusalError, match=r"^writable retained path$"):
        mounts.paths((writable,), (nested,), {writable.path}, deploy_service, RELEASE)
    mounts.paths((writable,), (writable,), {writable.path}, deploy_service, RELEASE)
    with pytest.raises(RefusalError, match=r"^incomplete writable set$"):
        mounts.paths((writable,), (), set(), deploy_service, RELEASE)


def test_exact_model_sources(deploy_service: Service) -> None:
    assert model.validate(kitchen(), deploy_service) == (
        mounts.Source(Path("/srv/o11y/data/secrets/token"), file=True, env=True),
        mounts.Source(Path("/srv/o11y/data/web"), writable=True),
        mounts.Source(Path(f"/srv/o11y/releases/{RELEASE}/config")),
        mounts.Source(Path("/srv/o11y/data/secrets/token"), file=True),
        mounts.Source(Path(f"/srv/o11y/releases/{RELEASE}/config/web"), file=True),
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
    source = mounts.Source(Path(path), writable=write, env=env)
    with pytest.raises(RefusalError) as caught:
        mounts.paths(
            (source,), (), {source.path} if write else set(), deploy_service, RELEASE
        )
    assert str(caught.value) == reason


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
    assert model.validate(candidate, deploy_service) == (
        mounts.Source(Path("/srv/o11y/data/web"), writable=True),
    )


def test_read_only_bind_cannot_alias_a_writable_bind(deploy_service: Service) -> None:
    path = Path("/srv/o11y/data/web")
    with pytest.raises(RefusalError, match=r"^path under writable bind$"):
        mounts.paths((mounts.Source(path),), (), {path}, deploy_service, RELEASE)


@pytest.mark.parametrize(
    "old",
    [
        mounts.Source(Path("/srv/o11y/data/web")),
        mounts.Source(Path("/srv/o11y/data/web/child"), writable=True),
    ],
)
def test_writable_bind_preserves_retained_readers_and_children(
    deploy_service: Service, old: mounts.Source
) -> None:
    source = mounts.Source(Path("/srv/o11y/data/web"), writable=True)
    with pytest.raises(RefusalError, match=r"^writable retained path$"):
        mounts.paths((source,), (old,), {source.path}, deploy_service, RELEASE)


def test_release_local_env_file_is_allowed(deploy_service: Service) -> None:
    source = mounts.Source(Path(f"/srv/o11y/releases/{RELEASE}/compose.env"), env=True)
    mounts.paths((source,), (), set(), deploy_service, RELEASE)


def test_ordinary_uppercase_and_dotted_components_are_allowed(
    deploy_service: Service,
) -> None:
    source = mounts.Source(Path("/srv/o11y/data/XX.XX"))
    mounts.paths(
        (source,), (), set(), replace(deploy_service, ro_binds=(source.path,)), RELEASE
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
    source = mounts.Source(Path(path))
    with pytest.raises(RefusalError):
        mounts.paths(
            (source,),
            (),
            set(),
            replace(deploy_service, ro_binds=(source.path,)),
            RELEASE,
        )


def test_read_only_permission_does_not_allow_env_file(deploy_service: Service) -> None:
    path = Path("/srv/o11y/data/settings.env")
    with pytest.raises(RefusalError, match=r"^unlisted env file$"):
        mounts.paths(
            (mounts.Source(path, file=True, env=True),),
            (),
            set(),
            replace(deploy_service, ro_binds=(path,)),
            RELEASE,
        )


@pytest.mark.parametrize(
    "source", ["data/config/file", "data/config/file/child", "data/config", "data"]
)
def test_writable_bind_cannot_overlap_read_only_control(
    deploy_service: Service, source: str
) -> None:
    allowed = replace(deploy_service, ro_binds=(Path("/srv/o11y/data/config/file"),))
    path = allowed.root / source
    with pytest.raises(RefusalError, match=r"^writable control path$"):
        mounts.paths(
            (mounts.Source(path, writable=True),), (), {path}, allowed, RELEASE
        )


def test_writable_bind_cannot_occupy_read_only_control_directory(
    deploy_service: Service,
) -> None:
    allowed = replace(deploy_service, ro_binds=(Path("/srv/o11y/data/config/file"),))
    path = allowed.root / "data/config/sibling"
    with pytest.raises(RefusalError, match=r"^writable control path$"):
        mounts.paths(
            (mounts.Source(path, writable=True),), (), {path}, allowed, RELEASE
        )


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
    assert mounts.retained(value) == (
        mounts.Source(Path("/srv/env"), file=True, env=True),
        mounts.Source(Path("/srv/rw"), writable=True),
        mounts.Source(Path("/srv/ro")),
        mounts.Source(Path("/srv/secret"), file=True),
        mounts.Source(Path("/srv/config"), file=True),
    )
    assert mounts.retained({"services": {"empty": {}}}) == ()


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("services", "web", "volumes", 0, "unknown"), True, "unknown object key"),
        (
            ("services", "web", "volumes", 0, "bind", "unknown"),
            True,
            "unknown object key",
        ),
        (("services", "web", "env_file"), None, "expected table list"),
        (("services", "web", "env_file", 0), None, "expected an object"),
        (("services", "web", "env_file", 0, "path"), None, "expected string"),
        (("services", "web", "env_file", 0, "required"), None, "optional env file"),
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
        (("secrets",), None, "expected an object"),
        (("secrets", "token"), None, "expected an object"),
        (("secrets", "token", "name"), None, "expected string"),
        (("secrets", "token", "file"), None, "expected string"),
        (("configs",), None, "expected an object"),
        (("configs", "config"), None, "expected an object"),
        (("configs", "config", "file"), None, "expected string"),
    ],
)
def test_nested_mounts_types(
    deploy_service: Service, path: tuple[str | int, ...], value: Json, reason: str
) -> None:
    with pytest.raises((RefusalError, ValueError)) as caught:
        model.validate(changed_kitchen(path, value), deploy_service)
    assert str(caught.value) == reason
