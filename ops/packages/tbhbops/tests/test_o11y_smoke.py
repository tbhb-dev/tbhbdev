"""Smoke expectations, including the `sh -c` fixtures' failure boundaries."""

import copy
from pathlib import Path

import pytest
import yaml

from tbhbops.o11y import init, smoke
lazy from tbhbops.o11y.validate import Document


def sentinels() -> smoke.Sentinels:
    return smoke.sentinels(
        (*(bytes(range(s.length)) for s in init.SECRETS), b"\xaa" * 16, b"\xbb" * 16)
    )


def test_sentinels_and_layout() -> None:
    sent = sentinels()
    values = dict(sent.values)
    for spec in init.SECRETS:
        assert init.valid(
            values[spec.name].decode().rstrip("\n"), spec.charset, spec.length
        )
    assert (
        values[init.WEBHOOK]
        == b"http://alert-stub:8080/cgi-bin/hook/12345/" + b"aa" * 16
    )
    assert values[init.SMTP] == b"bb" * 16
    assert sent.token == b"aa" * 16
    assert sent.scan_values() == (
        *(v.rstrip(b"\n") for v in values.values()),
        b"aa" * 16,
    )
    expected = smoke.layout(values, (123, 456))
    assert expected == {
        "alloy": (0, 0, 0o750, None),
        "grafana": (472, 472, 0o750, None),
        "loki": (10001, 10001, 0o750, None),
        "mimir": (0, 0, 0o750, None),
        "rustfs": (10001, 10001, 0o750, None),
        "secrets": (0, 0, 0o755, None),
        "tempo": (10001, 10001, 0o750, None),
        "secrets/rustfs.access_key": (10001, 10001, 0o400, 21),
        "secrets/rustfs.secret_key": (10001, 10001, 0o400, 41),
        "secrets/grafana_admin_password": (472, 472, 0o400, 33),
        "secrets/grafana_agents_password": (472, 472, 0o400, 33),
        "secrets/alert_discord_webhook_url": (472, 472, 0o400, 74),
        "secrets/alert_smtp_password": (472, 472, 0o400, 32),
        "secrets/s3.env": (123, 456, 0o600, 102),
    }
    assert smoke.layout_rule(expected, dict(expected)) is None
    assert (
        smoke.layout_rule({}, expected)
        == "owners, modes or sizes differ from the layout"
    )
    assert (
        smoke.s3_env(values)
        == b"AWS_ACCESS_KEY_ID="
        + values[init.SECRETS[0].name].rstrip(b"\n")
        + b"\nAWS_SECRET_ACCESS_KEY="
        + values[init.SECRETS[1].name]
    )
    with pytest.raises(ValueError, match=r"^invalid random draw length$"):
        smoke.sentinels((b"a", b"b", b"c", b"d", b"e"))


def test_scan_binary_and_sanitizer() -> None:
    needles = (b"Secret000", b"WebhookToken")
    assert smoke.scan(b"\xff\x00Secret000\xfe", needles) == "sentinel secret"
    assert smoke.scan(b"\xff\x00clean\xfe", needles) is None
    assert smoke.scan(b"", needles) == "scan empty"
    assert (
        smoke.sanitize_loki_error(
            "empty ring at /loki/wal/01 with Secret000\n! WebhookToken", needles
        )
        == "empty ring at [path] with [redacted] [redacted]"
    )
    assert smoke.sanitize_loki_error("\x00!", needles) == "none"
    assert smoke.sanitize_loki_error("A" * 121, needles) == "A" * 120
    assert smoke.sanitize_loki_error("a.,;:_=- 42", ()) == "a.,;:_=- 42"


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ("100 7", ("0.930000", True)),
        ("100000000000 25000000000", ("0.750000", False)),
        ("1000 109", ("0.891000", False)),
        ("10000000 1000001", ("0.900000", False)),
        ("1000 100\n", ("0.900000", True)),
        ("1000 50", ("0.950000", True)),
        ("1000 200", ("0.800000", False)),
        ("1 1", ("0.000000", False)),
        ("1 0", ("1.000000", True)),
        ("3 1", ("0.666667", False)),
        ("2\t1", ("0.500000", False)),
        ("000100 000007", ("0.930000", True)),
        ("999999999999 0", ("1.000000", True)),
        *[
            (value, None)
            for value in (
                "",
                "not counts",
                "0 0",
                "100 -1",
                "100 101",
                "100 7 extra",
                "100 7\n100 7",
                "/private/path",
                "1000000000000 1",
                "1 1000000000000",
                " 100 7",
                "100 7 ",
            )
        ],
    ],
)
def test_disk_records(counts: str, expected: tuple[str, bool] | None) -> None:
    assert smoke.disk_ratio(counts) == expected
    assert (
        smoke.disk_record(counts, "fixture") == "smoke Loki disk usage (fixture): "
        f"{expected[0] if expected else 'unavailable'}"
    )


@pytest.mark.parametrize(
    ("counts", "suffix"),
    [
        ("", "unavailable"),
        ("1000 200", "ratio=0.800000 below 0.90"),
        ("1000 50", "ratio=0.950000 at or above 0.90 (pressure at collection time)"),
    ],
)
@pytest.mark.parametrize(
    ("logs", "transitions", "ending"),
    [
        (b"", (), "inconclusive (no transition in head and tail windows)"),
        (
            b'private="value" ' + smoke.RESUME.encode(),
            (smoke.RESUME,),
            "inconclusive (no throttling transition in head and tail windows)",
        ),
        (smoke.THROTTLE.encode(), (smoke.THROTTLE,), "confirmed by transition line"),
        (
            (smoke.THROTTLE + smoke.RESUME).encode(),
            (smoke.THROTTLE, smoke.RESUME),
            "confirmed by transition line",
        ),
    ],
)
def test_disk_evidence(
    counts: str, suffix: str, logs: bytes, transitions: tuple[str, ...], ending: str
) -> None:
    assert smoke.disk_evidence(counts, logs) == (
        "smoke diagnostics: Loki disk usage " + suffix,
        *transitions,
        "smoke diagnostics: Loki WAL throttling " + ending,
    )


def response(wal: str = "    disk_full_threshold: 0.99", status: str = "200") -> str:
    return (
        f"unrelated: private\ningester:\n  wal:\n{wal}\n"
        f"other:\n  wal:\n    disk_full_threshold: 0.99\n\n{status}"
    )


def test_wal_effective_value() -> None:
    assert smoke.wal_threshold(0, response()) is None
    assert smoke.wal_threshold(0, response() + "\n") is None


@pytest.mark.parametrize("code", [0, 17, None])
@pytest.mark.parametrize("status", ["200", "201", "404", "500"])
@pytest.mark.parametrize(
    "wal",
    [
        "    disk_full_threshold: 0.99",
        "    disk_full_threshold: 0.90",
        "    other: 0.99",
        "    disk_full_threshold: private",
        "      disk_full_threshold: 0.99",
        "    disk_full_threshold: 0.99\n    disk_full_threshold: 0.99",
        "    disk_full_threshold: 0.99 extra",
    ],
)
def test_wal_fails_closed(code: int | None, status: str, wal: str) -> None:
    expected = (
        None
        if (code, status, wal) == (0, "200", "    disk_full_threshold: 0.99")
        else "configuration unavailable"
        if code != 0 or status != "200"
        else "missing or unexpected value"
    )
    assert smoke.wal_threshold(code, response(wal, status)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "200",
        "",
        "body\n200",
        "wal:\n    disk_full_threshold: 0.99\n200",
        "ingester:\n  wal:\n  other:\n    disk_full_threshold: 0.99\n200",
    ],
)
def test_wal_missing_structure(text: str) -> None:
    assert smoke.wal_threshold(0, text) is not None


def services() -> Document:
    return {
        **{
            s: {
                "depends_on": {
                    "s3-buckets": {"condition": "service_completed_successfully"}
                }
            }
            for s in ("loki", "tempo", "mimir")
        },
        "s3-buckets": {"depends_on": {"rustfs": {"condition": "service_healthy"}}},
    }


def test_ops_tables() -> None:
    from tbhbops.o11y import render  # noqa: PLC0415 - default rendered service set

    root = next(p for p in Path(__file__).parents if (p / "o11y/compose.yaml").exists())
    model = yaml.safe_load(
        render.substitute(
            (root / "o11y/compose.yaml").read_text(),
            render.Settings((), 50, email=False),
        )
    )["services"]
    actual = tuple(s for s in model if model[s].get("restart") == "unless-stopped")
    assert actual == smoke.SERVICES
    assert smoke.dependencies(model) is None
    assert smoke.PORTS == (13000, 14317, 14318)
    assert smoke.READY == {
        "rustfs": "http://127.0.0.1:9000/health/ready",
        "loki": "http://loki:3100/ready",
        "tempo": "http://tempo:3200/ready",
        "mimir": "http://mimir:8080/ready",
        "alloy": "http://alloy:12345/-/ready",
        "grafana": "http://grafana:3000/api/health",
    }
    assert smoke.LIMIT == 4194304
    assert tuple(smoke.SUCCESS) == ("1", "2", "3", "3a")


@pytest.mark.parametrize("service", ["loki", "tempo", "mimir", "s3-buckets"])
def test_dependencies(service: str) -> None:
    model = services()
    key = "rustfs" if service == "s3-buckets" else "s3-buckets"
    model[service]["depends_on"][key]["condition"] = "service_started"
    assert smoke.dependencies(model) == (
        "bucket health dependency"
        if service == "s3-buckets"
        else "backend bucket dependency"
    )
    del model[service]
    assert smoke.dependencies(model) == "missing bucket dependency"
    broken: Document = {service: None}
    assert smoke.dependencies(broken) == "missing bucket dependency"


def test_buckets() -> None:
    assert smoke.BUCKETS == ("loki", "mimir-blocks", "mimir-ruler", "tempo")
    for created in (smoke.BUCKETS, (), ("mimir-ruler",)):
        text = "noise\n" + "\r\n".join(
            f"bucket {name} {'created' if name in created else 'exists'}"
            for name in reversed(smoke.BUCKETS)
        )
        assert smoke.bucket_log(text, created) is None
        assert (
            smoke.bucket_log(text + "\nbucket unexpected exists", created)
            == "bucket bootstrap output"
        )
        assert (
            smoke.bucket_log(text.replace("loki", "missing"), created)
            == "bucket bootstrap output"
        )
    before = tuple(name + " timestamp" for name in smoke.BUCKETS)
    assert smoke.bucket_inventory("\n".join(reversed(before))) == before
    for bad in (
        "",
        "\n",
        "\n".join(before) + "\n\n",
        "\n".join(before[:-1]),
        "loki\nloki\nmimir-blocks\nmimir-ruler\ntempo",
    ):
        assert smoke.bucket_inventory(bad) is None
    assert smoke.kept_buckets(before, before) is None
    after = tuple(
        line.replace("mimir-ruler timestamp", "mimir-ruler new") for line in before
    )
    assert smoke.kept_buckets(before, after) is None
    assert (
        smoke.kept_buckets(before, ("loki new", *before[1:]))
        == "existing bucket recreated"
    )


def test_recreated_service_states() -> None:
    before = dict.fromkeys(smoke.SERVICES, ("old", "2026-10-25T05:38:26.51Z"))
    after = {
        **before,
        "loki": ("new", before["loki"][1]),
        "alloy": ("old", "2026-10-25T05:40:07Z"),
        "grafana": ("old", "2026-10-25T05:40:08Z"),
    }
    assert smoke.recreated(before, after) is None
    assert smoke.recreated({}, after) == "unexpected container state count"
    assert smoke.recreated(before, {}) == "unexpected container state count"
    broken = copy.deepcopy(after)
    broken["loki"] = before["loki"]
    assert smoke.recreated(before, broken) == "loki was not recreated"
    for service in smoke.SERVICES:
        broken = {**after, service: ("", after[service][1])}
        assert smoke.recreated(before, broken) == "malformed container state"
        if service in ("alloy", "grafana"):
            for value in (
                ("new", after[service][1]),
                before[service],
                ("old", "2026-10-25T05:38:26.5Z"),
            ):
                assert (
                    smoke.recreated(before, {**after, service: value})
                    == "dependent was not restarted in place"
                )
        elif service != "loki":
            for value in (("new", after[service][1]), ("old", "2026-10-25T05:40:00Z")):
                assert (
                    smoke.recreated(before, {**after, service: value})
                    == "unrelated service changed"
                )


def test_bucket_carriage_return_removal() -> None:
    text = (
        "bucket lo\rki created\nbucket mimir-blocks created\n"
        "bucket mimir-ruler created\nbucket tempo created\n"
    )
    assert smoke.bucket_log(text, smoke.BUCKETS) is None


def test_sentinel_boundaries_and_short_draws() -> None:
    sent = smoke.Sentinels((("one", b"X\n"), ("two", b"value \n")), b"token")
    assert sent.scan_values() == (b"X", b"value ", b"token")
    with pytest.raises(ValueError, match="zip"):
        smoke.sentinels((b"a" * 20, b"b" * 40))
    assert smoke.disk_ratio("128 127") == ("0.007813", False)
    assert (
        smoke.s3_env({init.SECRETS[0].name: b" X\n", init.SECRETS[1].name: b"X \n"})
        == b"AWS_ACCESS_KEY_ID= X\nAWS_SECRET_ACCESS_KEY=X \n"
    )


@pytest.mark.parametrize(
    "text",
    [
        response(status="200 "),
        response(status="200X"),
        "    disk_full_threshold: 0.99\n200",
        "ingester:\n    disk_full_threshold: 0.99\n200",
    ],
)
def test_wal_ignores_no_other_structure(text: str) -> None:
    assert smoke.wal_threshold(0, text) is not None


@pytest.mark.parametrize(
    ("code", "text", "expected"),
    [
        (0, "\n204", None),
        (0, "\n204\n", None),
        (7, "\n204", "transport-error"),
        (None, "\n204", "transport-error"),
        (0, "private\n500", "write-refused"),
        (0, "private\n503", "write-refused"),
        (0, "204", "malformed-status"),
        (0, "body\n20X", "malformed-status"),
        (0, "\n204 ", "malformed-status"),
        (0, "\n204X", "malformed-status"),
    ],
)
def test_loki_write_classification(
    code: int | None, text: str, expected: str | None
) -> None:
    assert smoke.loki_write_rule(code, text) == expected
    assert smoke.loki_write_rule(0, "multi\nline\n204") is None
