"""Inspect observability configuration and classify validator results."""

import json
import re
from collections import Counter
from typing import Any

# Parsed YAML and JSON carry heterogeneous nested structures.
lazy from collections.abc import Mapping

type Document = dict[str, Any]  # pyrefly: ignore[explicit-any]


EMPTY = ""
INVALID_DIVISOR = ("", 0)
SELF_PORT = 4320


def blocks(text: str, component: str) -> list[tuple[str, str]]:
    """Read top-level Alloy blocks; nested blocks end at indented braces."""
    return re.findall(
        rf'^{re.escape(component)} "([^"]+)" \{{\n(.*?)^}}',
        text,
        re.MULTILINE | re.DOTALL,
    )


def alloy(text: str) -> list[str]:
    findings = []
    scrapes = blocks(text, "prometheus.scrape")
    if not scrapes or any(
        not all(
            re.search(rf"^\s*{key}\s*=", block, re.MULTILINE)
            for key in ("sample_limit", "label_limit", "body_size_limit")
        )
        for _, block in scrapes
    ):
        findings.append("a scrape lacks a sample, label or body limit")
    writes = blocks(text, "prometheus.remote_write")
    if len(writes) != 1 or not all(
        re.search(rf"^\s*{key} \{{", writes[0][1], re.MULTILINE)
        for key in ("queue_config", "wal")
    ):
        findings.append("expected one remote write with queue_config and wal")
    receivers = dict(blocks(text, "otelcol.receiver.otlp"))
    if "self" not in receivers or "grpc" in receivers["self"]:
        findings.append("the self receiver is missing or takes gRPC")
    exporters = dict(blocks(text, "otelcol.exporter.otlphttp"))
    queue = exporters.get("mimir_self", EMPTY)
    if not re.search(r'\bsizer\s*= "items"', queue) or not re.search(
        r"\bqueue_size\s*= [0-9]+", queue
    ):
        findings.append("the self exporter queue is not bounded in items")
    return findings


def _ports(name: str, ports: object) -> list[str]:
    findings = []
    if not isinstance(ports, list):
        findings.append(f"{name}: ports must be a list")
        return findings
    published: list[object] = ports
    for port in published:
        if isinstance(port, str):
            local = port.startswith("127.0.0.1:")
            target = port.rpartition(":")[2].split("/")[0]
        elif isinstance(port, dict):
            local = port.get("host_ip") == "127.0.0.1"
            target = str(port.get("target"))
        else:
            local, target = False, str(port)
        if not local:
            findings.append(f"{name}: a port is not bound to 127.0.0.1")
        findings.extend(_target(name, target))
    return findings


def _target(name: str, target: str) -> list[str]:
    match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", target)
    if match is None:
        return [f"{name}: published targets must be literal ports or ranges"]
    if int(match[1]) <= SELF_PORT <= int(match[2] or match[1]):
        return [f"{name}: the self receiver on 4320 is published"]
    return []


def _service_settings(document: Document) -> list[str]:
    findings = []
    if "extends" in document:
        findings.append("extends is not supported")
    for name, service in document.get("services", {}).items():
        if "extends" in service:
            findings.append(f"{name}: extends is not supported")
        if service.get("network_mode") == "host":
            findings.append(f"{name}: host networking is not supported")
        findings.extend(_ports(name, service.get("ports", [])))
    return findings


def compose(base: Document, overlay: Document) -> list[str]:
    """Inspect safe-loaded YAML, refusing protected settings in overlays."""
    findings = []
    services = base.get("services", {})
    extra = overlay.get("services", {})
    for name, service in extra.items():
        if name in services and any(
            key in service for key in ("ports", "environment", "network_mode")
        ):
            findings.append(f"overlay changes protected settings for {name}")
    for name, document in (("compose.yaml", base), ("compose.tools.yaml", overlay)):
        if "include" in document:
            findings.append(f"{name}: include is not supported")
        findings.extend(_service_settings(document))
    service = services.get("alloy", {})
    environment = service.get("environment")
    if not isinstance(environment, dict) or not environment.get("GOMEMLIMIT"):
        findings.append("alloy: the container lacks GOMEMLIMIT")
    if not service.get("stop_grace_period"):
        findings.append("alloy: the container lacks stop_grace_period")
    return findings


def _objects(value: object) -> list[Document]:
    if isinstance(value, dict):
        mapping: dict[str, object] = value
        return [
            value,
            *(item for v in mapping.values() for item in _objects(v)),
        ]
    if isinstance(value, list):
        sequence: list[object] = value
        return [item for v in sequence for item in _objects(v)]
    return []


def dashboard(text: str, known: set[str]) -> list[str]:
    try:
        value: Document = json.loads(text)
    except ValueError:
        return ["does not parse as JSON"]
    if not isinstance(value, dict):
        return ["dashboard must be an object"]
    findings = [
        f"no {key}"
        for key in ("uid", "title")
        if not isinstance(value.get(key), str) or not value[key]
    ]
    return sorted(set(findings + _panels(value) + _references(value, known)))


def _panels(value: Document) -> list[str]:
    findings = []
    panels: list[Document] = value.get("panels", [])
    for panel in panels:
        ref = panel.get("datasource")
        if panel.get("type") != "row" and (
            not isinstance(ref, dict) or not isinstance(ref.get("uid"), str)
        ):
            name = panel.get("title", panel.get("id"))
            findings.append(f"panel {name} names no datasource uid")
    for identity, count in Counter(p.get("id") for p in panels).items():
        if count > 1:
            findings.append(f"panel id {identity} is not unique")
    return findings


def _references(value: Document, known: set[str]) -> list[str]:
    findings = []
    for obj in _objects(value):
        if "datasource" in obj:
            ref = obj["datasource"]
            if not isinstance(ref, dict):
                findings.append(
                    f"a datasource given as {json.dumps(ref)} rather than by uid"
                )
            else:
                reference: Document = ref
                if reference.get("uid") not in known:
                    findings.append(
                        f"datasource uid {reference.get('uid')} does not exist"
                    )
    return sorted(set(findings))


def dashboards(files: Mapping[str, str], known: set[str]) -> list[str]:
    findings = []
    identities = []
    if not known:
        findings.append("no datasource uid found in datasources.yaml")
    for name, text in files.items():
        findings.extend(f"{name}: {message}" for message in dashboard(text, known))
        try:
            value: Document = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("uid"), str):
            identities.append(value["uid"])
    findings.extend(
        f"uid used by more than one dashboard: {uid}"
        for uid, count in Counter(identities).items()
        if count > 1
    )
    return findings


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([mg])", value)
    if match is None:
        return 0
    return int(match[1]) * {"m": 1048576, "g": 1073741824}[match[2]]


MEMORY_COUNT = 6
SCRAPE_COUNT = 5
DOCKER_LAUNCH_FAILURE = 125


def alignment(
    base: Document, text: str, health: Document, alert: str, rules: str
) -> list[str]:
    """Keep dashboard divisors, alert bounds and secret mounts aligned."""
    services = base.get("services", {})
    expected = sorted(
        (name, memory_bytes(services.get(name, {}).get("mem_limit", EMPTY)))
        for name in ("alloy", "grafana", "loki", "mimir", "rustfs", "tempo")
    )
    actual = [
        _memory_divisor(expr)
        for expr in _targets(health, "Resident memory against mem_limit")
    ]
    samples = [
        _sample_divisor(expr)
        for expr in _targets(health, "Scraped samples against sample_limit")
    ]
    findings = []
    if (
        len(actual) != MEMORY_COUNT
        or sorted(actual) != expected
        or any(n == 0 for _, n in expected)
    ):
        findings.append(
            "memory panel divisors do not match six service mem_limit values"
        )
    expected_samples = []
    for _, block in blocks(text, "prometheus.scrape"):
        job = re.search(r'"job" = "([^"]+)"', block)
        limit = re.search(r"sample_limit\s*= ([0-9]+)", block)
        if job and limit:
            expected_samples.append((job[1], int(limit[1])))
    if len(expected_samples) != SCRAPE_COUNT or sorted(samples) != sorted(
        expected_samples
    ):
        findings.append(
            "sample panel divisors do not match five scrape sample_limit values"
        )
    return findings + _alerts(services, alert, rules)


def _targets(health: Document, title: str) -> list[str]:
    return [
        target.get("expr", EMPTY)
        for panel in health.get("panels", [])
        if panel.get("title") == title
        for target in panel.get("targets", [])
    ]


def _memory_divisor(expr: str) -> tuple[str, int]:
    match = re.fullmatch(
        r'(?:process_resident_memory_bytes\{job="o11y/([a-z]+)"\}'
        r"|max\(rustfs_system_process_resident_memory_bytes"
        r'\{job="o11y/(rustfs)"\}\)) / ([0-9]+)',
        expr,
    )
    return (match[1] or match[2], int(match[3])) if match else INVALID_DIVISOR


def _sample_divisor(expr: str) -> tuple[str, int]:
    match = re.fullmatch(
        r'scrape_samples_post_metric_relabeling\{job="(o11y/[a-z]+)"\}'
        r" / ([0-9]+)",
        expr,
    )
    return (match[1], int(match[2])) if match else INVALID_DIVISOR


def _alerts(services: Document, alert: str, rules: str) -> list[str]:
    findings = []
    if "url: $__file{/run/o11y/alert_discord_webhook_url}" not in alert:
        findings.append("alerting does not read the Discord webhook file")
    grafana = services.get("grafana", {})
    for secret in ("alert_discord_webhook_url", "alert_smtp_password"):
        mount = f"${{O11Y_DATA_DIR:?}}/secrets/{secret}:/run/o11y/{secret}:ro"
        if mount not in grafana.get("volumes", []):
            findings.append(f"grafana does not mount {secret} read-only")
    if (
        grafana.get("environment", {}).get("GF_SMTP_PASSWORD__FILE")
        != "/run/o11y/alert_smtp_password"
    ):
        findings.append("grafana does not set GF_SMTP_PASSWORD__FILE")
    rustfs = memory_bytes(services.get("rustfs", {}).get("mem_limit", EMPTY))
    if not rustfs or not re.search(
        r'rustfs_system_process_resident_memory_bytes\{job="o11y/rustfs"\}\) / '
        + str(rustfs)
        + r"$",
        rules,
        re.MULTILINE,
    ):
        findings.append("RustFS memory rule divisor does not match mem_limit")
    return findings


REJECTIONS = {
    "loki": "field not_a_loki_section not found",
    "tempo": "field not_a_tempo_section not found",
    "mimir": "field not_a_mimir_section not found",
    "alloy": 'component "otelcol.exporter.otlp.missing.input" does not exist',
}


def rejection(component: str, code: int, output: str) -> str | None:
    if code < 0 or code >= DOCKER_LAUNCH_FAILURE:
        return f"the validator did not run on the invalid fixture (exit {code})"
    if code == 0:
        return "no coverage, the invalid fixture was accepted"
    if REJECTIONS[component] not in output:
        return "the invalid fixture failed without naming what is broken in it"
    return None


# This exact error-line regex is safe only because Compose pins the images.
BELOW_GA = re.compile(
    r'^Error: [^ ]+: component "([^"]+)" is at stability level "[a-z-]+", '
    r'which is below the minimum allowed stability level "generally-available"\. '
    r'Use --stability\.level command-line flag to enable "[a-z-]+" features$'
)


def stability(code: int, output: str, flag: str) -> str | None:
    if code == 0:
        return (
            f"config.alloy passes at the default stability level, "
            f"so {flag} is not needed"
        )
    if code != 1:
        return f"the check at the default stability level did not run (exit {code})"
    closed = "Error: validation failed" in output.splitlines()
    below = set()
    for line in output.splitlines():
        if line == "Error: validation failed":
            continue
        if line.startswith("Error: "):
            match = BELOW_GA.fullmatch(line)
            if match is None:
                return (
                    "Alloy's output at the default stability level holds an "
                    "error this check doesn't recognize"
                )
            below.add(match[1])
    if not closed:
        return (
            "Alloy's output at the default stability level holds an "
            "error this check doesn't recognize"
        )
    if below != {"otelcol.processor.deltatocumulative"}:
        return (
            "config.alloy uses a component below generally "
            "available other than "
            "otelcol.processor.deltatocumulative"
        )
    return None


def docker_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    return {
        key: inherited[key]
        for key in ("PATH", "HOME", "TMPDIR", "DOCKER_CONFIG", "DOCKER_CONTEXT")
        if key in inherited
    }


def api_version(minimum: str) -> str | None:
    value = minimum.strip()
    if re.fullmatch(r"[0-9]+\.[0-9]+", value) and tuple(map(int, value.split("."))) >= (
        1,
        44,
    ):
        return value
    return None


def local_context(
    context: str, endpoint: str, home: str | None, *, linux: bool, socket: bool
) -> bool:
    allowed = (
        ("/var/run/docker.sock", "/run/docker.sock")
        if linux
        else ("/var/run/docker.sock",)
    )
    if not linux and home:
        allowed = (*allowed, f"{home}/.docker/run/docker.sock")
    return (
        context.strip() in ("default", "desktop-linux")
        and endpoint.strip() in tuple("unix://" + path for path in allowed)
        and socket
    )


def dedicated_engine(  # noqa: PLR0911, PLR0913  # one refusal per explicit metadata rule
    named: str,
    context: str,
    endpoint: str,
    *,
    socket: bool,
    containers: list[str],
    volumes: list[str],
    networks: list[str],
) -> str | None:
    """Accept only a named Unix socket for an empty per-run engine."""
    if not named.startswith("unix:///"):
        return "the dedicated engine must name an absolute Unix socket"
    if context != "default":
        return "the dedicated engine requires the default Docker context"
    if endpoint.strip() != named:
        return "the Docker endpoint does not match the dedicated engine"
    if not socket:
        return "the dedicated engine endpoint is not a socket"
    if containers:
        return "the dedicated engine already holds containers"
    if volumes:
        return "the dedicated engine already holds volumes"
    if any(name not in ("bridge", "host", "none") for name in networks):
        return "the dedicated engine already holds custom networks"
    return None


def local_identity(info: str, hostname: str, *, linux: bool) -> bool:
    name, _, operating_system = info.strip().partition("|")
    return (
        name == hostname
        if linux
        else name == "docker-desktop" and operating_system == "Docker Desktop"
    )
