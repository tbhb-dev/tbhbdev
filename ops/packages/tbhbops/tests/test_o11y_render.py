"""Tables port render.bats validation, substitution and release naming cases."""

import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest
from tbhbops.o11y import render

ENV = {
    "O11Y_DATA_DIR": "/srv/data",
    "O11Y_GRAFANA_ROOT_URL": "https://grafana.example.invalid/",
    "O11Y_GRAFANA_OWNER_LOGIN": "owner@example.invalid",
}
MAIL = {
    "O11Y_ALERT_EMAIL_TO": "to@example.invalid",
    "O11Y_ALERT_SMTP_HOST": "smtp.example.invalid",
    "O11Y_ALERT_SMTP_PORT": "587",
    "O11Y_ALERT_SMTP_USER": "user@example.invalid",
    "O11Y_ALERT_SMTP_FROM": "from@example.invalid",
}


def settings(**overrides: str) -> render.Settings:
    result = render.check(ENV | overrides)
    assert isinstance(result, render.Settings)
    return result


@pytest.mark.parametrize("value", ["", "a", "Z9", "host-c-o11y", "a" * 63])
def test_tailnet_hostname(value: str) -> None:
    result = settings(O11Y_TS_HOSTNAME=value)
    assert dict(result.values).get("O11Y_TS_HOSTNAME", "") == value


@pytest.mark.parametrize("value", ["", "true", "false"])
def test_fixed_addresses(value: str) -> None:
    assert (
        dict(settings(O11Y_FIXED_ADDRESSES=value).values).get(
            "O11Y_FIXED_ADDRESSES", ""
        )
        == value
    )


@pytest.mark.parametrize("value", ["", "/tls", "/srv/o11y/tls", "/a_B/0-9"])
def test_tls_cert_directory(value: str) -> None:
    result = settings(O11Y_GRAFANA_TLS_CERT_DIR=value)
    assert dict(result.values).get("O11Y_GRAFANA_TLS_CERT_DIR", "") == value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        *(("O11Y_TS_HOSTNAME", v) for v in ("-a", "a-", "a.b", "a_b", "a" * 64)),
        *(("O11Y_FIXED_ADDRESSES", v) for v in ("TRUE", "0", "true\n")),
        ("O11Y_TS_HOSTNAME", "a\n"),
        *(
            ("O11Y_GRAFANA_TLS_CERT_DIR", v)
            for v in (
                "tls",
                "/",
                "/tls/",
                "//tls",
                "/a//tls",
                "/a/../tls",
                "/a/./tls",
                "/a b",
                "/tls:rw",
                "/tls\n",
                "/a/$key",
                "/a\\b",
            )
        ),
    ],
)
def test_network_inputs_reject_values_privately(name: str, value: str) -> None:
    assert render.check(ENV | {name: value}) == render.Errors(
        (f"{name} has an invalid format",)
    )


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("O11Y_TS_HOSTNAME", "", "off\n"),
        ("O11Y_TS_HOSTNAME", "sidecar", "on\n"),
        ("O11Y_FIXED_ADDRESSES", "", "off\n"),
        ("O11Y_FIXED_ADDRESSES", "false", "off\n"),
        ("O11Y_FIXED_ADDRESSES", "true", "on\n"),
        ("O11Y_GRAFANA_TLS_CERT_DIR", "", "off\n"),
        ("O11Y_GRAFANA_TLS_CERT_DIR", "/tls", "on\n"),
    ],
)
def test_network_ranges(name: str, value: str, expected: str) -> None:
    text = (
        f"# @{name}@ begin\non\n# @{name}@ end\n"
        f"# @{name}@ off begin\noff\n# @{name}@ off end\n"
    )
    assert render.substitute(text, settings(**{name: value})) == expected


def test_tables_defaults_and_frozen_results() -> None:
    assert tuple(ENV) == render.REQUIRED
    optional = (
        "O11Y_GRAFANA_PORT O11Y_OTLP_GRPC_PORT O11Y_OTLP_HTTP_PORT "
        "O11Y_SECRETS_OWNER O11Y_CLUSTER O11Y_HOST_NAME O11Y_ENVIRONMENT"
    )
    assert tuple(optional.split()) == render.OPTIONAL
    assert (*MAIL, "O11Y_ALERT_SMTP_SKIP_VERIFY") == render.EMAIL
    result = settings()
    assert result == render.Settings(
        (*ENV.items(), ("O11Y_LOKI_WAL_DISK_FULL_THRESHOLD", "0.90")), 50, email=False
    )
    with pytest.raises(FrozenInstanceError):
        result.__setattr__("email", True)
    errors = render.Errors(("test",))
    with pytest.raises(FrozenInstanceError):
        errors.__setattr__("messages", ())


@pytest.mark.parametrize("name", render.REQUIRED)
@pytest.mark.parametrize("value", [None, ""])
def test_unset_required_is_named(name: str, value: str | None) -> None:
    env = ENV.copy()
    if value is None:
        del env[name]
    else:
        env[name] = value
    assert render.check(env) == render.Errors((f"{name} is not set",))


def test_all_missing_are_reported_before_other_validation() -> None:
    assert render.check({}) == render.Errors(
        tuple(f"{name} is not set" for name in ENV)
    )


@pytest.mark.parametrize(
    "value",
    [
        "bad login@example.invalid",
        'bad"@example.invalid',
        "a@b,c@d",
        "a@b\n",
        "@b",
        "a@",
        "a/b@c",
    ],
)
def test_owner_login_invalid_without_value(value: str) -> None:
    assert render.check(
        ENV | {"O11Y_GRAFANA_OWNER_LOGIN": value, "O11Y_DISK_RESERVE_GB": "bad"}
    ) == render.Errors(("O11Y_GRAFANA_OWNER_LOGIN must be one email address",))


@pytest.mark.parametrize(
    "value", ["08", "x", "1.5", "-1", "1234567", "00", "1\n", " 1", "\u0661"]
)
def test_bad_reserve(value: str) -> None:
    assert render.check(
        ENV | {"O11Y_DISK_RESERVE_GB": value, **MAIL, "O11Y_ALERT_SMTP_PORT": "bad"}
    ) == render.Errors(
        ("O11Y_DISK_RESERVE_GB must be a whole number of GiB, with no leading zero",)
    )


@pytest.mark.parametrize(
    ("value", "expected"), [("", 50), ("0", 0), ("1", 1), ("7", 7), ("999999", 999999)]
)
def test_reserve_boundaries(value: str, expected: int) -> None:
    assert settings(O11Y_DISK_RESERVE_GB=value).reserve_gb == expected


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("O11Y_ALERT_EMAIL_TO", "a@b,c@d", "must be one email address"),
        (
            "O11Y_ALERT_SMTP_HOST",
            "",
            "must be a host name, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_HOST",
            "-host",
            "must be a host name, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_HOST",
            "host-",
            "must be a host name, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_HOST",
            "a..b",
            "must be a host name, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_HOST",
            "a_b",
            "must be a host name, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_PORT",
            "",
            "must be a port from 1 to 65535, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_PORT",
            "0",
            "must be a port from 1 to 65535, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_PORT",
            "01",
            "must be a port from 1 to 65535, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_PORT",
            "65536",
            "must be a port from 1 to 65535, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_PORT",
            "100000",
            "must be a port from 1 to 65535, since O11Y_ALERT_EMAIL_TO is set",
        ),
        (
            "O11Y_ALERT_SMTP_USER",
            "",
            (
                "must be a login of letters, digits and ._%+@-, since "
                "O11Y_ALERT_EMAIL_TO is set"
            ),
        ),
        (
            "O11Y_ALERT_SMTP_USER",
            "bad user",
            (
                "must be a login of letters, digits and ._%+@-, since "
                "O11Y_ALERT_EMAIL_TO is set"
            ),
        ),
        (
            "O11Y_ALERT_SMTP_FROM",
            "",
            "must be one email address, since O11Y_ALERT_EMAIL_TO is set",
        ),
        ("O11Y_ALERT_SMTP_SKIP_VERIFY", "TRUE", "must be true or false"),
    ],
)
def test_email_validation(name: str, value: str, message: str) -> None:
    assert render.check(ENV | MAIL | {name: value}) == render.Errors(
        (f"{name} {message}",)
    )


def test_recipient_without_smtp_names_every_failure() -> None:
    result = render.check(ENV | {"O11Y_ALERT_EMAIL_TO": "to@example.invalid"})
    assert isinstance(result, render.Errors)
    assert tuple(m.split()[0] for m in result.messages) == render.EMAIL[1:5]
    assert all("to@example.invalid" not in m for m in result.messages)


@pytest.mark.parametrize("port", ["1", "65535"])
@pytest.mark.parametrize("skip", ["", "true", "false"])
def test_email_valid_boundaries(port: str, skip: str) -> None:
    result = settings(
        **(
            MAIL
            | {
                "O11Y_ALERT_SMTP_PORT": port,
                "O11Y_ALERT_SMTP_SKIP_VERIFY": skip,
                "O11Y_ALERT_SMTP_HOST": "a.b-c.9",
                "O11Y_ALERT_SMTP_USER": "aZ09._%+@-",
                "O11Y_ALERT_SMTP_FROM": "aZ09._%+-@aZ09.-",
            }
        )
    )
    assert result.email is True
    expected = MAIL | {
        "O11Y_ALERT_SMTP_PORT": port,
        "O11Y_ALERT_SMTP_HOST": "a.b-c.9",
        "O11Y_ALERT_SMTP_USER": "aZ09._%+@-",
        "O11Y_ALERT_SMTP_FROM": "aZ09._%+-@aZ09.-",
    }
    pairs = tuple(expected.items())
    if skip:
        pairs += (("O11Y_ALERT_SMTP_SKIP_VERIFY", skip),)
    assert result.values == (
        *settings().values,
        *pairs,
        ("O11Y_ALERT_SMTP_ENABLED", "true"),
    )


@pytest.mark.parametrize("name", render.OPTIONAL)
@pytest.mark.parametrize("value", ["", "override"])
def test_overrides_only_when_set(name: str, value: str) -> None:
    result = settings(**{name: value})
    expected = settings().values
    if value:
        expected += ((name, value),)
    assert result.values == expected


@pytest.mark.parametrize("value", ["", "0.99"])
def test_wal_default_override(value: str) -> None:
    assert settings(O11Y_LOKI_WAL_DISK_FULL_THRESHOLD=value).values[-1] == (
        "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD",
        value or "0.90",
    )


@pytest.mark.parametrize(
    ("email", "text", "expected"),
    [
        (False, "@O11Y_DISK_RESERVE_GB@ @O11Y_DISK_RESERVE_BYTES@\n", "7 7516192768\n"),
        (
            False,
            (
                "keep\n# @O11Y_ALERT_EMAIL@ begin\non\n# "
                "@O11Y_ALERT_EMAIL@ end\n# @O11Y_ALERT_EMAIL@ off "
                "begin\noff\n# @O11Y_ALERT_EMAIL@ off end\n"
            ),
            "keep\noff\n",
        ),
        (
            True,
            (
                "keep\n# @O11Y_ALERT_EMAIL@ begin\non\n# "
                "@O11Y_ALERT_EMAIL@ end\n# @O11Y_ALERT_EMAIL@ off "
                "begin\noff\n# @O11Y_ALERT_EMAIL@ off end\n"
            ),
            "keep\non\n",
        ),
        pytest.param(
            False,
            "keep\n# @O11Y_ALERT_EMAIL@ begin\nrest\n",
            "keep\n",
            id="unterminated-range",
        ),
        pytest.param(
            False,
            "# @O11Y_ALERT_EMAIL@ begin\nfirst\nsecond\n"
            "# @O11Y_ALERT_EMAIL@ end\nkeep\n",
            "keep\n",
            id="sed-multiple-interior-lines",
        ),
        pytest.param(
            False,
            "# @O11Y_ALERT_EMAIL@ begin # @O11Y_ALERT_EMAIL@ "
            "end\nremove\n# @O11Y_ALERT_EMAIL@ end\nkeep\n",
            "keep\n",
            id="sed-start-line-end-is-ignored",
        ),
        (
            False,
            (
                "# @O11Y_ALERT_EMAIL@ begin\n# @O11Y_ALERT_EMAIL@ "
                "begin\n# @O11Y_ALERT_EMAIL@ end\nkeep\n# "
                "@O11Y_ALERT_EMAIL@ end\n"
            ),
            "keep\n",
        ),
        (
            False,
            (
                "# @O11Y_ALERT_EMAIL@ begin\nremove\n# "
                "@O11Y_ALERT_EMAIL@ end\nkeep\n# @O11Y_ALERT_EMAIL@ "
                "begin\nremove\n# @O11Y_ALERT_EMAIL@ end\n"
            ),
            "keep\n",
        ),
        (
            True,
            "prefix # @O11Y_ALERT_EMAIL@ unknown\n@O11Y_OTHER@\nlast",
            "@O11Y_OTHER@\nlast",
        ),
        (False, "", ""),
        pytest.param(
            False,
            (
                "keep\r# @O11Y_ALERT_EMAIL@ begin\nremove\n"
                "# @O11Y_ALERT_EMAIL@ end\nlast\vword"
            ),
            "last\vword",
            id="sed-only-newline-delimits-lines",
        ),
    ],
)
def test_substitute(email: bool, text: str, expected: str) -> None:
    assert (
        render.substitute(
            text, replace(settings(O11Y_DISK_RESERVE_GB="7"), email=email)
        )
        == expected
    )


def test_email_off_ignores_invalid_smtp() -> None:
    assert (
        settings(O11Y_ALERT_SMTP_PORT="bad", O11Y_ALERT_SMTP_SKIP_VERIFY="bad")
        == settings()
    )


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"hello": b"hello\n"},
        {
            "z": b"\x00\xff",
            "a/nested": b"one",
            "Z": b"two",
            "é": b"three",
            "MANIFEST.sha256": b"ignored",
            "a/MANIFEST.sha256": b"ignored",
        },
    ],
)
def test_manifest_and_independent_target(files: dict[str, bytes]) -> None:
    expected = "\n".join(
        f"{hashlib.sha256(data).hexdigest()}  ./{name}"
        for name, data in sorted(files.items(), key=lambda item: item[0].encode())
        if name.split("/")[-1] != "MANIFEST.sha256"
    )
    assert render.manifest(files) == expected
    name = "tiny-" + hashlib.sha256((expected + "\n").encode()).hexdigest()[:12]
    assert render.target_name("tiny", expected) == name


def test_known_tiny_name() -> None:
    text = hashlib.sha256(b"hello\n").hexdigest() + "  ./hello"
    expected = "tiny-" + hashlib.sha256((text + "\n").encode()).hexdigest()[:12]
    assert expected == "tiny-7f75fd412dd2"
    assert (
        render.target_name("tiny", render.manifest({"hello": b"hello\n"})) == expected
    )


@pytest.mark.parametrize("email", [False, True])
def test_env_line_order(email: bool) -> None:
    chosen = settings(
        **(MAIL if email else {}), **dict.fromkeys(render.OPTIONAL, "override")
    )
    assert render.env_lines(chosen, {"z": "z-123", "a": "a-456"}, "/remote/config") == (
        *(f"{name}={value}" for name, value in ENV.items()),
        "O11Y_LOKI_WAL_DISK_FULL_THRESHOLD=0.90",
        *(f"{name}=override" for name in render.OPTIONAL),
        *(
            (
                *(f"{name}={value}" for name, value in MAIL.items()),
                "O11Y_ALERT_SMTP_ENABLED=true",
            )
            if email
            else ()
        ),
        "O11Y_CONFIG_A=/remote/config/a-456",
        "O11Y_CONFIG_Z=/remote/config/z-123",
    )


def test_raw_filename_byte_order_and_hash() -> None:
    files = {"é": b"second", "\udc80": b"first"}
    text = (
        hashlib.sha256(b"first").hexdigest()
        + "  ./\udc80\n"
        + hashlib.sha256(b"second").hexdigest()
        + "  ./é"
    )
    assert render.manifest(files) == text
    digest = hashlib.sha256((text + "\n").encode(errors="surrogateescape")).hexdigest()[
        :12
    ]
    assert render.target_name("raw", text) == f"raw-{digest}"
    assert render.env_lines(settings(), {"é": "second", "\udc80": "first"}, "/config")[
        -2:
    ] == ("O11Y_CONFIG_\udc80=/config/first", "O11Y_CONFIG_É=/config/second")
