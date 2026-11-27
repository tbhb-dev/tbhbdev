"""Static observability checks and pure Docker result classifiers."""

import json
from pathlib import Path

import pytest
import yaml
from tbhbops.o11y import validate as v

ALLOY = """prometheus.scrape "test" {
 sample_limit = 10
 label_limit = 40
 body_size_limit = "16MiB"
}
prometheus.remote_write "mimir" {
 endpoint {
  queue_config {
  }
 }
 wal {
 }
}
otelcol.receiver.otlp "self" {
 http {
 }
}
otelcol.exporter.otlphttp "mimir_self" {
 sending_queue {
  sizer = "items"
  queue_size = 20000
 }
}
"""


def test_alloy_valid() -> None:
    assert v.alloy(ALLOY) == []
    assert v.blocks(ALLOY, "prometheus.scrape") == [
        ("test", "\n".join(ALLOY.splitlines()[1:4]) + "\n")
    ]


@pytest.mark.parametrize("key", ["sample_limit", "label_limit", "body_size_limit"])
def test_each_scrape_limit(key: str) -> None:
    assert v.alloy(ALLOY.replace(key, "removed")) == [
        "a scrape lacks a sample, label or body limit"
    ]
    assert v.alloy(
        ALLOY
        + ALLOY.split("prometheus.remote_write", maxsplit=1)[0].replace(key, "removed")
    ) == ["a scrape lacks a sample, label or body limit"]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("prometheus.scrape", "other", "a scrape lacks a sample, label or body limit"),
        (
            "prometheus.remote_write",
            "other",
            "expected one remote write with queue_config and wal",
        ),
        (
            "queue_config",
            "other",
            "expected one remote write with queue_config and wal",
        ),
        ("wal {", "other {", "expected one remote write with queue_config and wal"),
        ('"self"', '"other"', "the self receiver is missing or takes gRPC"),
        ("http {", "grpc {", "the self receiver is missing or takes gRPC"),
        ('"mimir_self"', '"other"', "the self exporter queue is not bounded in items"),
        ('"items"', '"bytes"', "the self exporter queue is not bounded in items"),
        ("queue_size", "other", "the self exporter queue is not bounded in items"),
    ],
)
def test_alloy_failures(old: str, new: str, message: str) -> None:
    assert v.alloy(ALLOY.replace(old, new)) == [message]


def test_two_remote_writes() -> None:
    assert v.alloy(ALLOY + 'prometheus.remote_write "second" {\n}\n') == [
        "expected one remote write with queue_config and wal"
    ]


def base() -> v.Document:
    return {
        "services": {
            "alloy": {
                "environment": {"GOMEMLIMIT": "600MiB"},
                "stop_grace_period": "30s",
            }
        }
    }


def test_compose_anchors_and_long_ports() -> None:
    document = yaml.safe_load("""bounds: &bounds
  environment: {GOMEMLIMIT: 600MiB}
  stop_grace_period: 30s
services:
  alloy:
    <<: *bounds
    ports: ["127.0.0.1:4317:4317"]
""")
    assert (
        v.compose(
            document,
            {
                "services": {
                    "tool": {"ports": [{"host_ip": "127.0.0.1", "target": 3000}]}
                }
            },
        )
        == []
    )


@pytest.mark.parametrize(
    "port",
    [
        4320,
        "4320",
        "4320:4320",
        "${BIND}:4320:4320",
        "[::1]:4320:4320",
        {"target": 4320},
        {"host_ip": "${BIND}", "target": "4320"},
    ],
)
def test_bad_ports(port: object) -> None:
    assert v.compose(base(), {"services": {"tool": {"ports": [port]}}}) == [
        "tool: a port is not bound to 127.0.0.1",
        "tool: the self receiver on 4320 is published",
    ]


@pytest.mark.parametrize(
    "port", ["127.0.0.1:4320:4320/tcp", {"host_ip": "127.0.0.1", "target": 4320}]
)
def test_loopback_self_port(port: object) -> None:
    assert v.compose(base(), {"services": {"tool": {"ports": [port]}}}) == [
        "tool: the self receiver on 4320 is published"
    ]


@pytest.mark.parametrize("target", ["4319-4321", "4320-4321", "4319-4320"])
def test_self_port_in_range(target: str) -> None:
    assert v.compose(
        base(), {"services": {"tool": {"ports": ["127.0.0.1:5000-5002:" + target]}}}
    ) == ["tool: the self receiver on 4320 is published"]


@pytest.mark.parametrize("target", ["4318-4319", "4321-4322"])
def test_ranges_away_from_self(target: str) -> None:
    assert (
        v.compose(
            base(), {"services": {"tool": {"ports": ["127.0.0.1:5000-5001:" + target]}}}
        )
        == []
    )


def test_variable_target_refused() -> None:
    assert v.compose(
        base(), {"services": {"tool": {"ports": ["127.0.0.1:5000:${TARGET:-4320}"]}}}
    ) == ["tool: published targets must be literal ports or ranges"]


@pytest.mark.parametrize("ports", [None, {}, "127.0.0.1:3000:3000", 3000])
def test_ports_must_be_list(ports: object) -> None:
    b = base()
    b["services"]["alloy"]["ports"] = ports
    assert v.compose(b, {}) == ["alloy: ports must be a list"]


@pytest.mark.parametrize(
    ("key", "value"), [("ports", []), ("environment", {}), ("network_mode", "bridge")]
)
def test_overlay_protected(key: str, value: object) -> None:
    assert v.compose(base(), {"services": {"alloy": {key: value}}}) == [
        "overlay changes protected settings for alloy"
    ]
    assert v.compose(base(), {"services": {"alloy": {"command": ["run"]}}}) == []


def test_extends_and_network() -> None:
    assert v.compose(
        base() | {"extends": {}},
        {"services": {"tool": {"extends": {}, "network_mode": "host"}}},
    ) == [
        "extends is not supported",
        "tool: extends is not supported",
        "tool: host networking is not supported",
    ]


@pytest.mark.parametrize("env", [{}, {"GOMEMLIMIT": ""}, [], None])
def test_missing_gomemlimit(env: object) -> None:
    b = base()
    b["services"]["alloy"]["environment"] = env
    assert v.compose(b, {}) == ["alloy: the container lacks GOMEMLIMIT"]


def test_missing_grace() -> None:
    b = base()
    del b["services"]["alloy"]["stop_grace_period"]
    assert v.compose(b, {}) == ["alloy: the container lacks stop_grace_period"]
    assert v.compose({}, {}) == [
        "alloy: the container lacks GOMEMLIMIT",
        "alloy: the container lacks stop_grace_period",
    ]


def test_dashboards() -> None:
    good: v.Document = {
        "uid": "test",
        "title": "Test",
        "panels": [
            {
                "id": 1,
                "title": "Panel",
                "type": "timeseries",
                "datasource": {"uid": "mimir"},
            },
            {"id": 2, "type": "row"},
        ],
    }
    text = json.dumps(good)
    assert v.dashboard(text, {"mimir"}) == []
    assert v.dashboards({"nested/a.json": text, "other/b.json": text}, {"mimir"}) == [
        "uid used by more than one dashboard: test"
    ]
    assert v.dashboards({}, set()) == ["no datasource uid found in datasources.yaml"]
    assert v.dashboards({"bad.json": "{"}, {"mimir"}) == [
        "bad.json: does not parse as JSON"
    ]
    assert v.dashboards({"bad.json": "[]", "missing.json": "{}"}, {"mimir"}) == [
        "bad.json: dashboard must be an object",
        "missing.json: no title",
        "missing.json: no uid",
    ]
    assert v.dashboard("{", set()) == ["does not parse as JSON"]
    assert v.dashboard("[]", set()) == ["dashboard must be an object"]
    assert v.dashboard(json.dumps({"uid": 1, "title": ""}), set()) == [
        "no title",
        "no uid",
    ]
    panel = good["panels"][0]
    panel["datasource"] = "mimir"
    good["panels"].append({"id": 1})
    assert v.dashboard(json.dumps(good), {"mimir"}) == sorted(
        [
            'a datasource given as "mimir" rather than by uid',
            "panel Panel names no datasource uid",
            "panel 1 names no datasource uid",
            "panel id 1 is not unique",
        ]
    )
    panel["datasource"] = {}
    assert "datasource uid None does not exist" in v.dashboard(
        json.dumps(good), {"mimir"}
    )
    fixture = Path(__file__).with_name("invalid_dashboard.json").read_text()
    assert v.dashboard(fixture, {"mimir"}) == [
        "datasource uid not-a-datasource does not exist"
    ]


def repo_tree() -> Path:
    return next(
        parent / "o11y"
        for parent in Path(__file__).parents
        if (parent / "o11y/compose.yaml").is_file()
    )


def real_inputs() -> tuple[v.Document, str, v.Document, str, str]:
    root = repo_tree()
    provisioning = root / "config/grafana/provisioning"
    return (
        yaml.safe_load((root / "compose.yaml").read_text()),
        (root / "config/alloy/config.alloy").read_text(),
        json.loads(
            (root / "config/grafana/dashboards/general/stack-health.json").read_text()
        ),
        (provisioning / "alerting/alerting.yaml").read_text(),
        (provisioning / "alerting/rules.yaml").read_text(),
    )


def inline_inputs() -> tuple[v.Document, str, v.Document, str, str]:
    names = ("alloy", "grafana", "loki", "mimir", "rustfs", "tempo")
    services: v.Document = {name: {"mem_limit": "768m"} for name in names}
    services["grafana"].update(
        environment={"GF_SMTP_PASSWORD__FILE": "/run/o11y/alert_smtp_password"},
        volumes=[
            f"${{O11Y_DATA_DIR:?}}/secrets/{secret}:/run/o11y/{secret}:ro"
            for secret in ("alert_discord_webhook_url", "alert_smtp_password")
        ],
    )
    text = "".join(
        f'prometheus.scrape "{name}" {{\n'
        f' targets = [{{"job" = "o11y/{name}"}}]\n'
        " sample_limit = 3000\n}\n"
        for name in names
        if name != "rustfs"
    )
    health: v.Document = {
        "panels": [
            {
                "title": "Resident memory against mem_limit",
                "targets": [
                    {
                        "expr": (
                            f'process_resident_memory_bytes{{job="o11y/{name}"}}'
                            " / 805306368"
                        )
                    }
                    for name in names
                ],
            },
            {
                "title": "Scraped samples against sample_limit",
                "targets": [
                    {
                        "expr": (
                            f'scrape_samples_post_metric_relabeling{{job="o11y/{name}"}}'
                            " / 3000"
                        )
                    }
                    for name in names
                    if name != "rustfs"
                ],
            },
        ]
    }
    return (
        {"services": services},
        text,
        health,
        "url: $__file{/run/o11y/alert_discord_webhook_url}",
        'rustfs_system_process_resident_memory_bytes{job="o11y/rustfs"}) / 805306368\n',
    )


def test_real_tree() -> None:
    root = repo_tree()
    b, text, health, alert, rules = real_inputs()
    known = {
        x["uid"]
        for x in yaml.safe_load(
            (
                root / "config/grafana/provisioning/datasources/datasources.yaml"
            ).read_text()
        )["datasources"]
    }
    files = {
        str(p.relative_to(root / "config/grafana/dashboards")): p.read_text()
        for p in (root / "config/grafana/dashboards").rglob("*.json")
    }
    assert v.alloy(text) == []
    assert v.compose(b, yaml.safe_load((root / "compose.tools.yaml").read_text())) == []
    assert v.dashboards(files, known) == []
    assert v.alignment(b, text, health, alert, rules) == []


@pytest.fixture(name="metrics_queries_rule")
def load_metrics_queries_rule() -> v.Document:
    path = repo_tree() / "config/grafana/provisioning/alerting/rules.yaml"
    rules = yaml.safe_load(path.read_text())
    return next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["uid"] == "o11y-metrics-queries-failing"
    )


@pytest.mark.parametrize(
    "state", ["execErrState", "noDataState"], ids=["query-error", "no-data"]
)
def test_metrics_queries_failing_alerts_after_ten_minutes(
    metrics_queries_rule: v.Document, state: str
) -> None:
    assert metrics_queries_rule["for"] == "10m"
    assert metrics_queries_rule[state] == "Alerting"


def test_metrics_queries_failing_probe_and_routing(
    metrics_queries_rule: v.Document,
) -> None:
    query, condition = metrics_queries_rule["data"]
    assert query["datasourceUid"] == "mimir"
    assert query["model"]["instant"] is True
    assert query["model"]["expr"] == 'count(up{namespace="o11y"} offset 26h)'
    assert condition["refId"] == metrics_queries_rule["condition"]
    assert condition["model"]["expression"] == query["refId"]
    assert condition["model"]["conditions"] == [
        {"evaluator": {"type": "lt", "params": [1]}}
    ]
    path = repo_tree() / "config/grafana/provisioning/alerting/alerting.yaml"
    routing = yaml.safe_load(path.read_text())
    assert routing["policies"][0]["receiver"] == "o11y"
    assert any(
        point["name"] == "o11y" and point["receivers"]
        for point in routing["contactPoints"]
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("768m", 805306368),
        ("1g", 1073741824),
        ("2g", 2147483648),
        ("garbage", 0),
        ("", 0),
    ],
)
def test_memory(value: str, expected: int) -> None:
    assert v.memory_bytes(value) == expected


@pytest.mark.parametrize(
    "change",
    [
        "memory",
        "memory-count",
        "mem-limit",
        "sample",
        "sample-count",
        "sample-block",
        "sample-job",
        "alert",
        "discord-mount",
        "smtp-mount",
        "smtp-env",
        "rule",
        "rustfs-limit",
    ],
)
def test_alignment_failures(change: str) -> None:
    b, text, health, alert, rules = inline_inputs()
    memory = next(
        p for p in health["panels"] if p["title"] == "Resident memory against mem_limit"
    )
    sample = next(
        p
        for p in health["panels"]
        if p["title"] == "Scraped samples against sample_limit"
    )
    if change == "memory":
        memory["targets"][0]["expr"] = "bad"
    if change == "memory-count":
        memory["targets"].pop()
    if change == "mem-limit":
        b["services"]["alloy"]["mem_limit"] = "bad"
    if change == "sample":
        sample["targets"][0]["expr"] = "bad"
    if change == "sample-count":
        sample["targets"].pop()
    if change == "sample-block":
        text = text.replace("sample_limit", "removed")
    if change == "sample-job":
        text = text.replace('"job" =', '"removed" =')
    alert, rules = change_alerts(change, b, alert, rules)
    assert v.alignment(b, text, health, alert, rules)
    assert v.alignment({}, "", {}, "", "") == [
        "memory panel divisors do not match six service mem_limit values",
        "sample panel divisors do not match five scrape sample_limit values",
        "alerting does not read the Discord webhook file",
        "grafana does not mount alert_discord_webhook_url read-only",
        "grafana does not mount alert_smtp_password read-only",
        "grafana does not set GF_SMTP_PASSWORD__FILE",
        "RustFS memory rule divisor does not match mem_limit",
    ]


def change_alerts(
    change: str, b: v.Document, alert: str, rules: str
) -> tuple[str, str]:
    if change == "alert":
        alert = ""
    if change in ("discord-mount", "smtp-mount"):
        secret = (
            "alert_discord_webhook_url"
            if change == "discord-mount"
            else "alert_smtp_password"
        )
        b["services"]["grafana"]["volumes"] = [
            x for x in b["services"]["grafana"]["volumes"] if secret not in x
        ]
    if change == "smtp-env":
        b["services"]["grafana"]["environment"].pop("GF_SMTP_PASSWORD__FILE")
    if change == "rule":
        rules = ""
    if change == "rustfs-limit":
        b["services"]["rustfs"]["mem_limit"] = "bad"
    return alert, rules


@pytest.mark.parametrize("component", ["loki", "tempo", "mimir", "alloy"])
@pytest.mark.parametrize("code", [125, 126, 127, 137, -9])
def test_no_rejection_without_run(component: str, code: int) -> None:
    assert (
        v.rejection(component, code, v.REJECTIONS[component])
        == f"the validator did not run on the invalid fixture (exit {code})"
    )


@pytest.mark.parametrize("component", ["loki", "tempo", "mimir", "alloy"])
def test_rejection(component: str) -> None:
    assert (
        v.rejection(component, 0, "") == "no coverage, the invalid fixture was accepted"
    )
    assert (
        v.rejection(component, 1, "")
        == "the invalid fixture failed without naming what is broken in it"
    )
    assert (
        v.rejection(
            component, 1, "before\n" + v.REJECTIONS[component] + " or is out of scope\n"
        )
        is None
    )


def diagnostic(component: str = "otelcol.processor.deltatocumulative") -> str:
    return (
        f'Error: /etc/alloy/config.alloy:1:1: component "{component}" '
        'is at stability level "experimental", which is below the minimum '
        'allowed stability level "generally-available". '
        'Use --stability.level command-line flag to enable "experimental" features\n'
    )


def test_stability() -> None:
    good = diagnostic() + "\n1 | example\nError: validation failed\n"
    assert v.stability(1, good, "flag") is None
    assert v.stability(1, good + diagnostic(), "flag") is None
    assert (
        v.stability(0, "", "flag")
        == "config.alloy passes at the default stability level, so flag is not needed"
    )
    assert (
        v.stability(137, good, "flag")
        == "the check at the default stability level did not run (exit 137)"
    )
    other = (
        "config.alloy uses a component below generally "
        "available other than "
        "otelcol.processor.deltatocumulative"
    )
    assert v.stability(1, good + diagnostic("otelcol.exporter.debug"), "flag") == other
    assert v.stability(1, "Error: validation failed\n", "flag") == other
    unknown = (
        "Alloy's output at the default stability level holds an "
        "error this check doesn't recognize"
    )
    assert v.stability(1, diagnostic(), "flag") == unknown
    assert (
        v.stability(
            1,
            good
            + (
                "Error: /etc/alloy/config.alloy:9:1: component "
                '"otelcol.exporter.debug" is at stability level '
                '"experimental"\n'
            ),
            "flag",
        )
        == unknown
    )


def test_environment() -> None:
    allowed = {
        key: key
        for key in ("PATH", "HOME", "TMPDIR", "DOCKER_CONFIG", "DOCKER_CONTEXT")
    }
    dropped = dict.fromkeys(
        (
            "O11Y_BIND_ADDR",
            "O11Y_S3_ENV_FILE",
            "O11Y_DATA_DIR",
            "O11Y_GRAFANA_PORT",
            "COMPOSE_PROFILES",
            "COMPOSE_FILE",
            "DOCKER_HOST",
            "DOCKER_API_VERSION",
            "O11Y_COMPOSE",
            "O11Y_SMOKE_LEAK",
            "O11Y_SMOKE_STOP_AFTER",
        ),
        "sentinel",
    )
    assert v.docker_environment(allowed | dropped) == allowed
    assert v.docker_environment({}) == {}


@pytest.mark.parametrize(
    ("minimum", "expected"),
    [
        ("1.40", None),
        ("1.43", None),
        ("1.44", "1.44"),
        ("1.45\n", "1.45"),
        ("2.0", "2.0"),
        ("", None),
        ("bad", None),
    ],
)
def test_api(minimum: str, expected: str | None) -> None:
    assert v.api_version(minimum) == expected


@pytest.mark.parametrize(
    ("linux", "path"),
    [
        (True, "/var/run/docker.sock"),
        (True, "/run/docker.sock"),
        (False, "/var/run/docker.sock"),
        (False, "/home/operator/.docker/run/docker.sock"),
    ],
)
def test_local_context(linux: bool, path: str) -> None:
    for context in ("default", "desktop-linux\n"):
        assert v.local_context(
            context, "unix://" + path, "/home/operator", linux=linux, socket=True
        )
    assert not v.local_context(
        "remote", "unix://" + path, "/home/operator", linux=linux, socket=True
    )
    assert not v.local_context(
        "default", "unix://" + path, "/home/operator", linux=linux, socket=False
    )
    assert not v.local_context(
        "default", "tcp://localhost:2375", "/home/operator", linux=linux, socket=True
    )


def test_identity() -> None:
    assert v.local_identity("host|Ubuntu\n", "host", linux=True)
    assert not v.local_identity("other|Ubuntu", "host", linux=True)
    assert v.local_identity("docker-desktop|Docker Desktop\n", "host", linux=False)
    assert not v.local_identity("host|Docker Desktop", "host", linux=False)
    assert not v.local_identity("docker-desktop|other", "host", linux=False)
    assert v.local_identity("host|Ubuntu|extra", "host", linux=True)


def test_local_context_without_home() -> None:
    assert v.local_context(
        "default", "unix:///var/run/docker.sock", None, linux=False, socket=True
    )
    assert not v.local_context(
        "default", "unix:///.docker/run/docker.sock", None, linux=False, socket=True
    )
    assert not v.local_context(
        "default", "unix:///run/docker.sock", "/operator", linux=False, socket=True
    )
    assert not v.local_context(
        "default",
        "unix:///operator/.docker/run/docker.sock",
        "/operator",
        linux=True,
        socket=True,
    )


def test_dashboard_continues_after_parse_error() -> None:
    text = '{"uid":"duplicate","title":"Title"}'
    assert v.dashboards({"invalid": "{", "one": text, "two": text}, {"mimir"}) == [
        "invalid: does not parse as JSON",
        "uid used by more than one dashboard: duplicate",
    ]


def test_alignment_missing_queries_and_zero_limits() -> None:
    b, text, health, alert, rules = inline_inputs()
    memory = next(
        p for p in health["panels"] if p["title"] == "Resident memory against mem_limit"
    )
    memory["targets"][0].pop("expr")
    assert v.alignment(b, text, health, alert, rules) == [
        "memory panel divisors do not match six service mem_limit values"
    ]
    memory.pop("targets")
    assert v.alignment(b, text, health, alert, rules) == [
        "memory panel divisors do not match six service mem_limit values"
    ]
    for name in ("alloy", "grafana", "loki", "mimir", "rustfs", "tempo"):
        b["services"][name]["mem_limit"] = "0m"
    memory["targets"] = [
        {"expr": f'process_resident_memory_bytes{{job="o11y/{name}"}} / 0'}
        for name in ("alloy", "grafana", "loki", "mimir", "rustfs", "tempo")
    ]
    assert v.alignment(b, text, health, alert, rules) == [
        "memory panel divisors do not match six service mem_limit values",
        "RustFS memory rule divisor does not match mem_limit",
    ]


def test_classifier_boundary() -> None:
    assert v.rejection("alloy", 124, v.REJECTIONS["alloy"]) is None
    assert v.stability(2, diagnostic() + "Error: validation failed\n", "flag") == (
        "the check at the default stability level did not run (exit 2)"
    )


@pytest.mark.parametrize("filename", ["compose.yaml", "compose.tools.yaml"])
@pytest.mark.parametrize("include", [[], ["other.yaml"], None])
def test_compose_include_refused(filename: str, include: object) -> None:
    documents = {"compose.yaml": base(), "compose.tools.yaml": {}}
    documents[filename]["include"] = include
    assert v.compose(documents["compose.yaml"], documents["compose.tools.yaml"]) == [
        f"{filename}: include is not supported"
    ]


@pytest.mark.parametrize(
    (
        "named",
        "context",
        "endpoint",
        "socket",
        "containers",
        "volumes",
        "networks",
        "expected",
    ),
    [
        (
            "tcp://engine:2375",
            "default",
            "tcp://engine:2375",
            True,
            [],
            [],
            [],
            "the dedicated engine must name an absolute Unix socket",
        ),
        (
            "ssh://engine",
            "default",
            "ssh://engine",
            True,
            [],
            [],
            [],
            "the dedicated engine must name an absolute Unix socket",
        ),
        (
            "unix://relative.sock",
            "default",
            "unix://relative.sock",
            True,
            [],
            [],
            [],
            "the dedicated engine must name an absolute Unix socket",
        ),
        (
            "unix:///run/dind/docker.sock",
            "desktop-linux",
            "unix:///run/dind/docker.sock",
            True,
            [],
            [],
            [],
            "the dedicated engine requires the default Docker context",
        ),
        (
            "unix:///run/dind/docker.sock",
            "default\n",
            "unix:///run/dind/docker.sock",
            True,
            [],
            [],
            [],
            "the dedicated engine requires the default Docker context",
        ),
        (
            "unix:///run/dind/docker.sock",
            "default",
            "unix:///other.sock",
            True,
            [],
            [],
            [],
            "the Docker endpoint does not match the dedicated engine",
        ),
        (
            "unix:///run/dind/docker.sock",
            "default",
            "unix:///run/dind/docker.sock",
            False,
            [],
            [],
            [],
            "the dedicated engine endpoint is not a socket",
        ),
        (
            "unix:///run/dind/docker.sock",
            "default",
            "unix:///run/dind/docker.sock",
            True,
            ["stopped-container"],
            [],
            [],
            "the dedicated engine already holds containers",
        ),
        (
            "unix:///run/dind/docker.sock",
            "default",
            "unix:///run/dind/docker.sock",
            True,
            [],
            ["orphan-volume"],
            [],
            "the dedicated engine already holds volumes",
        ),
        (
            "unix:///run/dind/docker.sock",
            "default",
            "unix:///run/dind/docker.sock",
            True,
            [],
            [],
            ["bridge", "custom"],
            "the dedicated engine already holds custom networks",
        ),
        (
            "unix:///run/dind/docker.sock",
            "default",
            "  unix:///run/dind/docker.sock\n",
            True,
            [],
            [],
            ["bridge", "host", "none"],
            None,
        ),
        (
            "unix:///run/dind/docker.sock",
            "default",
            "unix:///run/dind/docker.sock",
            True,
            [],
            [],
            [],
            None,
        ),
    ],
)
def test_dedicated_engine(  # decision table columns
    named: str,
    context: str,
    endpoint: str,
    socket: bool,
    containers: list[str],
    volumes: list[str],
    networks: list[str],
    expected: str | None,
) -> None:
    assert (
        v.dedicated_engine(
            named,
            context,
            endpoint,
            socket=socket,
            containers=containers,
            volumes=volumes,
            networks=networks,
        )
        == expected
    )


def test_agents_overview_has_trace_searches_per_service() -> None:
    dashboard = json.loads(
        (repo_tree() / "config/grafana/dashboards/agents/overview.json").read_text()
    )
    panels = dashboard["panels"]
    assert len({panel["id"] for panel in panels}) == len(panels)
    traces = [p for p in panels if p["datasource"]["uid"] == "tempo"]
    queries = {
        p["title"]: (p["targets"][0]["query"], p["targets"][0]["limit"], p["gridPos"])
        for p in traces
    }
    assert sorted(queries) == [
        "Recent Antigravity steps",
        "Recent Antigravity traces",
        "Recent Claude Code traces",
        "Recent Codex traces",
    ]
    for title in ("Recent Antigravity traces", "Recent Antigravity steps"):
        agy, agy_limit, _ = queries[title]
        assert (
            agy == '{ resource.service.name = "antigravity" } with (most_recent=true)'
        )
        assert agy_limit == 20
    claude, claude_limit, claude_pos = queries["Recent Claude Code traces"]
    codex, codex_limit, codex_pos = queries["Recent Codex traces"]
    assert 'resource.service.name="claude-code"' in claude
    assert 'resource.service.name="codex_exec"' in codex
    assert claude_limit == codex_limit == 20
    assert (claude_pos["x"], claude_pos["w"]) == (0, 12)
    assert (codex_pos["x"], codex_pos["w"]) == (12, 12)
    assert claude_pos["y"] == codex_pos["y"]


def test_alloy_drops_session_ids_from_metric_datapoints() -> None:
    config = (repo_tree() / "config/alloy/config.alloy").read_text()
    transform = config.split('otelcol.processor.transform "resource" {')[1].split(
        "log_statements"
    )[0]
    assert 'delete_key(attributes, "session.id")' in transform
    assert 'delete_key(attributes, "session_id")' in transform
