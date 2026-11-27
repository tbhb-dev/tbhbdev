"""Validate normalized Compose fields and resource bounds."""

import pytest
from tbhbops.deploy import model, model_fields
from tbhbops.deploy.protocol import Json, RefusalError, mapping
lazy from tbhbops.deploy.config import Service

from ...deploy_model import changed_kitchen, minimal


def test_string_map_defaults_do_not_treat_options_as_labels() -> None:
    model_fields.string_map({"com.docker.compose.project": "value"})
    with pytest.raises(RefusalError, match=r"^mapping value type$"):
        model_fields.string_map({"key": None})


def test_valid_empty_resource_and_logging_defaults(deploy_service: Service) -> None:
    assert model.validate(minimal(deploy={}), deploy_service) == ()
    with pytest.raises(RefusalError, match=r"^logging driver$"):
        model.validate(minimal(logging={"driver": "journald"}), deploy_service)


def test_ulimits_after_a_numeric_entry_are_checked(deploy_service: Service) -> None:
    with pytest.raises(RefusalError, match=r"^wrong value type$"):
        model.validate(
            minimal(ulimits={"nofile": 10, "nproc": {"soft": False}}), deploy_service
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
    assert model.validate(minimal(stop_grace_period=duration), deploy_service) == ()


@pytest.mark.parametrize(
    "duration", ["60.001s", "1m1s", "1h", "60001ms", "60000001us", "60000000001ns"]
)
def test_stop_grace_ceiling(deploy_service: Service, duration: str) -> None:
    with pytest.raises(RefusalError, match=r"^stop grace ceiling$"):
        model.validate(minimal(stop_grace_period=duration), deploy_service)


@pytest.mark.parametrize("duration", ["", "forever", "61", "-1s", "1s-junk", "NaNs"])
def test_stop_grace_unparseable(deploy_service: Service, duration: str) -> None:
    with pytest.raises(RefusalError, match=r"^stop grace duration$"):
        model.validate(minimal(stop_grace_period=duration), deploy_service)


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
        model.validate(value, deploy_service)
    assert str(caught.value) == reason


@pytest.mark.parametrize("memory", ["0", "0m", "-1", "unlimited", "", "1.0"])
def test_memory_limit_must_be_positive(deploy_service: Service, memory: str) -> None:
    with pytest.raises(RefusalError, match=r"^positive size required$"):
        model.validate(minimal(mem_limit=memory), deploy_service)


@pytest.mark.parametrize("pids", [0, -1])
def test_pid_limit_must_be_positive(deploy_service: Service, pids: int) -> None:
    with pytest.raises(RefusalError, match=r"^integer below minimum$"):
        model.validate(minimal(pids_limit=pids), deploy_service)


def test_pid_limit_accepts_one_process(deploy_service: Service) -> None:
    assert model.validate(minimal(pids_limit=1), deploy_service) == ()


@pytest.mark.parametrize("memory", ["1", "1024", "001024", "1K", "1g", "2M", "1b"])
def test_memory_limit_alternate_spelling(deploy_service: Service, memory: str) -> None:
    value = minimal(deploy={"resources": {"limits": {"memory": memory}}})
    del mapping(mapping(value["services"])["web"])["mem_limit"]
    assert model.validate(value, deploy_service) == ()


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
        model.validate(minimal(logging=logging), deploy_service)
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
        model.validate(minimal(logging=logging), deploy_service)


def test_logging_without_options_reports_missing_rotation(
    deploy_service: Service,
) -> None:
    with pytest.raises(RefusalError, match=r"^expected string$"):
        model.validate(minimal(logging={"driver": "json-file"}), deploy_service)


@pytest.mark.parametrize("primary", ["0", "1024"])
def test_conflicting_memory_limits_cannot_enable_unlimited(
    deploy_service: Service, primary: str
) -> None:
    with pytest.raises(RefusalError, match=r"^positive size required$"):
        model.validate(
            minimal(
                mem_limit=primary, deploy={"resources": {"limits": {"memory": "0"}}}
            ),
            deploy_service,
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
        model.validate(
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
        model.validate(
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
        model.validate(minimal(memswap_limit=value), deploy_service)


@pytest.mark.parametrize("value", ["1", "001024", "1b", "2M", "1g"])
def test_memswap_limit_accepts_memory_sizes(
    deploy_service: Service, value: str
) -> None:
    assert model.validate(minimal(memswap_limit=value), deploy_service) == ()


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
                model.validate(candidate, deploy_service)
        else:
            assert model.validate(candidate, deploy_service) == ()


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
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
        (("services", "web", "command"), "wrong", "expected a string list"),
        (("services", "web", "command", 0), None, "expected strings"),
        (("services", "web", "entrypoint"), "wrong", "expected a string list"),
        (("services", "web", "environment"), None, "expected an object"),
        (("services", "web", "environment", "VAR"), 1, "mapping value type"),
        (("services", "web", "environment", "EMPTY"), 1, "mapping value type"),
        (("services", "web", "labels"), None, "expected an object"),
        (("services", "web", "labels", "owner"), None, "mapping value type"),
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
    ],
)
def test_nested_model_fields_types(
    deploy_service: Service, path: tuple[str | int, ...], value: Json, reason: str
) -> None:
    with pytest.raises((RefusalError, ValueError)) as caught:
        model.validate(changed_kitchen(path, value), deploy_service)
    assert str(caught.value) == reason
