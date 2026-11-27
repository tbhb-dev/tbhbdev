"""Exercise the real project inputs and every process seam without Docker."""

import argparse
import io
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import override
lazy from collections.abc import Mapping, Sequence

import pytest
import yaml

from tbhbops import dns_smoke as smoke

ROOT = next(
    p for p in Path(__file__).resolve().parents if (p / "apps/dns/compose.yaml").is_file()
)
SOURCE = ROOT / "apps/dns"
COMPOSE = (SOURCE / "compose.yaml").read_text(encoding="utf-8")
LAN = """nas-a IN A 198.51.100.41
nas-b IN A 198.51.100.106
"""
TEMPLATE = """nas-a IN A @NAS-A_TAILNET_IPV4@
nas-b IN A @NAS-B_TAILNET_IPV4@
"""
COREFILE = """s3.example.invalid:53 {
    bind {$DNS_BIND_LAN} {$DNS_BIND_TAILNET}
    file /zones/lan.zone
}
"""
ZONE_TEMPLATES = {
    "ca.example.invalid.zone": "@ IN A 192.0.2.44\n",
    "otlp.example.invalid.zone.tmpl": "@ IN A @O11Y_TAILNET_IPV4@\n",
    "grafana.example.invalid.zone.tmpl": "@ IN A @O11Y_TAILNET_IPV4@\n",
}
STAGE = Path("/temporary/dns-smoke")
DIG_PIN = (
    "internetsystemsconsortium/bind9:9.20"
    "@sha256:66092edb53c081fa11b1fa8219e99065b0f497cb38f568ba789702a4c10e2590"
)

SUCCESS_OUTPUT = "".join(
    f"ok {view}: {name}\n"
    for view in ("lan", "tailnet", "other")
    for name in (
        "nas-a.s3.example.invalid",
        "nas-b.s3.example.invalid",
        "ca.example.invalid",
        "ns.ca.example.invalid",
        "otlp.example.invalid",
        "ns.otlp.example.invalid",
        "grafana.example.invalid",
        "ns.grafana.example.invalid",
        "example.invalid",
    )
    for _ in range(1 if name == "example.invalid" else 2)
)


@dataclass(frozen=True)
class Result:
    code: int | None = 0
    stdout: str = ""
    stderr: str = ""


def reply(status: str, name: str, address: str = "") -> str:
    answer = f"{name}. 60 IN A {address}\n" if address else ""
    return f";; ->>HEADER<<- opcode: QUERY, status: {status}, id: 42\n{answer}"


@dataclass
class Runner:
    calls: list[tuple[list[str], dict[str, str], int]] = field(default_factory=list)
    fail_at: int = -1
    retry: bool = False
    refuse_lan: bool = False

    def run(  # noqa: PLR0911  # one result for each simulated Docker operation
        self, argv: Sequence[str], env: Mapping[str, str], timeout: int
    ) -> Result:
        self.calls.append((list(argv), dict(env), timeout))
        if len(self.calls) == self.fail_at:
            return Result(1, "failure output\n", "failure stderr\n")
        if argv[1] == "logs":
            return Result(stdout="server logs\n", stderr="server stderr\n")
        if argv[1] != "run":
            return Result()
        if self.retry:
            self.retry = False
            return Result(None)
        network = argv[argv.index("--network") + 1]
        name = argv[-7]
        if (
            network == "dns-smoke-other"
            or self.refuse_lan
            or (
                network == "dns-smoke-lan"
                and not name.endswith(".s3.example.invalid")
                and name != "example.invalid"
            )
        ):
            return Result(stdout=reply("REFUSED", name))
        if name == "example.invalid":
            return Result(stdout=reply("NXDOMAIN", name))
        if argv[-6] == "AAAA":
            return Result(stdout=reply("NOERROR", name))
        addresses = {
            "dns-smoke-lan": {
                "nas-a.s3.example.invalid": "198.51.100.41",
                "nas-b.s3.example.invalid": "198.51.100.106",
            },
            "dns-smoke-tailnet": {
                "nas-a.s3.example.invalid": "192.0.2.41",
                "nas-b.s3.example.invalid": "192.0.2.42",
                "ca.example.invalid": "192.0.2.44",
                "ns.ca.example.invalid": "192.0.2.44",
                "otlp.example.invalid": "192.0.2.80",
                "ns.otlp.example.invalid": "192.0.2.80",
                "grafana.example.invalid": "192.0.2.80",
                "ns.grafana.example.invalid": "192.0.2.80",
            },
        }
        return Result(stdout=reply("NOERROR", name, addresses[network][name]))


@dataclass
class Files:
    data: dict[Path, str] = field(
        default_factory=lambda: {
            SOURCE / "compose.yaml": COMPOSE,
            SOURCE / "config/Corefile": COREFILE,
            SOURCE / "zones/lan.zone": LAN,
            SOURCE / "zones/tailnet.zone.tmpl": TEMPLATE,
            **{SOURCE / "zones" / name: zone for name, zone in ZONE_TEMPLATES.items()},
        }
    )
    writes: list[tuple[Path, str | bytes, int]] = field(default_factory=list)
    directories: list[Path] = field(default_factory=list)
    modes: dict[Path, int] = field(default_factory=dict)
    error: Exception | None = None

    def temporary_directory(self) -> Path:
        if self.error:
            raise self.error
        return STAGE

    def mkdir(self, path: Path) -> None:
        self.directories.append(path)

    def chmod(self, path: Path, mode: int) -> None:
        self.modes[path] = mode

    def read_text(self, path: Path) -> str:
        return self.data[path]

    def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None:
        assert isinstance(text, str)
        self.writes.append((path, text, mode))
        self.data[path] = text


@dataclass
class System:
    env: dict[str, str] = field(default_factory=lambda: {"PATH": "/bin"})
    filesystem: Files = field(default_factory=Files)
    runner: Runner = field(default_factory=Runner)
    clock: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds


def args() -> argparse.Namespace:
    return argparse.Namespace(
        source=SOURCE,
        artifacts=Path("/artifacts"),
        dedicated_engine="unix:///run/dind/docker.sock",
    )


def test_environment() -> None:
    keep = {
        key: key.lower()
        for key in (
            (
                "PATH",
                "HOME",
                "DOCKER_HOST",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
                "DOCKER_CONFIG",
            )
        )
    }
    assert smoke.docker_environment(keep | {"UNRELATED": "omit"}, None) == keep
    assert smoke.docker_environment({}, None) == {}
    assert smoke.docker_environment(keep, "unix:///dedicated") == keep | {
        "DOCKER_HOST": "unix:///dedicated"
    }


def test_compose_settings_and_no_image_version_coupling() -> None:
    image, command = smoke.service_command(COMPOSE, STAGE)
    assert (
        command
        == (
            "create --name dns-smoke-coredns --network dns-smoke-lan --ip 198.51.100.2 "
            "--cap-drop ALL --cap-add NET_BIND_SERVICE "
            "--security-opt no-new-privileges:true --read-only --memory 256m "
            "-e DNS_BIND_LAN=198.51.100.2 -e DNS_BIND_TAILNET=192.0.2.2 "
            f"-e DNS_LAN_TAILNET_ADDRS= -v {STAGE}/config:/etc/coredns:ro "
            f"-v {STAGE}/zones:/zones:ro {image} -conf /etc/coredns/Corefile"
        ).split()
    )
    assert image == yaml.safe_load(COMPOSE)["services"]["coredns"]["image"]
    changed = (
        COMPOSE.replace("read_only: true", "read_only: false")
        .replace("NET_BIND_SERVICE", "CHOWN")
        .replace("256m", "128m")
    )
    _, changed_command = smoke.service_command(changed, STAGE)
    assert "--read-only" not in changed_command
    assert "CHOWN" in changed_command
    assert "128m" in changed_command
    with pytest.raises(smoke.SmokeError) as unpinned:
        smoke.service_command(COMPOSE.replace("@sha256:", "@invalid:"), STAGE)
    assert str(unpinned.value) == "CoreDNS image must be digest-pinned"


def test_render_and_records() -> None:
    rendered = smoke.render_tailnet_zone(TEMPLATE)
    assert smoke.zone_records(rendered) == {
        "nas-a.s3.example.invalid": "192.0.2.41",
        "nas-b.s3.example.invalid": "192.0.2.42",
    }
    assert smoke.zone_records(LAN) == {
        "nas-a.s3.example.invalid": "198.51.100.41",
        "nas-b.s3.example.invalid": "198.51.100.106",
    }
    with pytest.raises(smoke.SmokeError) as unresolved:
        smoke.render_tailnet_zone("; ignore @NAME@ in comments\nhost IN A @UNKNOWN@\n")
    assert str(unresolved.value) == "tailnet zone has unreplaced placeholders"
    with pytest.raises(smoke.SmokeError) as empty:
        smoke.zone_records("; no records")
    assert str(empty.value) == "zone has no A records"


@pytest.mark.parametrize(
    ("view", "prefix", "addresses"),
    [
        ("lan", "198.51.100", ("198.51.100.41", "198.51.100.106")),
        ("tailnet", "192.0.2", ("192.0.2.41", "192.0.2.42")),
        ("other", "203.0.113", ("", "")),
    ],
)
@pytest.mark.parametrize("record_type", ["A", "AAAA"])
def test_s3_query_plan(
    view: str, prefix: str, addresses: tuple[str, str], record_type: str
) -> None:
    checks = smoke.queries(LAN, smoke.render_tailnet_zone(TEMPLATE))
    selected = [
        query
        for query in checks
        if query.view == view
        and query.record_type == record_type
        and query.name.endswith(".s3.example.invalid")
    ]
    assert selected == [
        smoke.Query(
            view,
            prefix,
            f"{host}.s3.example.invalid",
            frozenset({"NOERROR" if address else "REFUSED"}),
            frozenset({address}) if address and record_type == "A" else frozenset(),
            record_type,
        )
        for host, address in zip(("nas-a", "nas-b"), addresses, strict=True)
    ]


@pytest.mark.parametrize(
    ("view", "prefix", "statuses"),
    [
        ("lan", "198.51.100", {"NOERROR", "NXDOMAIN", "SERVFAIL"}),
        ("tailnet", "192.0.2", {"NOERROR", "NXDOMAIN", "SERVFAIL"}),
        ("other", "203.0.113", {"REFUSED"}),
    ],
)
def test_forward_query_plan(view: str, prefix: str, statuses: set[str]) -> None:
    checks = smoke.queries(LAN, smoke.render_tailnet_zone(TEMPLATE))
    assert [
        query
        for query in checks
        if query.view == view and query.name == "example.invalid"
    ] == [
        smoke.Query(view, prefix, "example.invalid", frozenset(statuses), frozenset())
    ]


def test_dig_command() -> None:
    checks = smoke.queries(LAN, smoke.render_tailnet_zone(TEMPLATE))
    assert (
        smoke.dig_command(checks[0])
        == (
            "run --rm --network dns-smoke-lan --ip 198.51.100.3 "
            f"--entrypoint dig {DIG_PIN} @198.51.100.2 nas-a.s3.example.invalid "
            "A +time=2 +tries=1 +noall +comments +answer"
        ).split()
    )


@pytest.mark.parametrize(
    ("view", "prefix", "status"),
    [
        ("lan", "198.51.100", "REFUSED"),
        ("tailnet", "192.0.2", "NOERROR"),
        ("other", "203.0.113", "REFUSED"),
    ],
)
@pytest.mark.parametrize(
    "record",
    [
        ("ca.example.invalid", "192.0.2.44"),
        ("ns.ca.example.invalid", "192.0.2.44"),
        ("otlp.example.invalid", "192.0.2.80"),
        ("ns.otlp.example.invalid", "192.0.2.80"),
        ("grafana.example.invalid", "192.0.2.80"),
        ("ns.grafana.example.invalid", "192.0.2.80"),
    ],
)
@pytest.mark.parametrize("record_type", ["A", "AAAA"])
def test_tailnet_only_query_plan(
    view: str, prefix: str, status: str, record: tuple[str, str], record_type: str
) -> None:
    name, address = record
    checks = smoke.queries(LAN, smoke.render_tailnet_zone(TEMPLATE))
    assert len(checks) == 51
    selected = [
        query
        for query in checks
        if query.view == view
        and query.name == name
        and query.record_type == record_type
    ]
    assert selected == [
        smoke.Query(
            view=view,
            prefix=prefix,
            name=name,
            statuses=frozenset({status}),
            answers=frozenset({address})
            if status == "NOERROR" and record_type == "A"
            else frozenset(),
            record_type=record_type,
        )
    ]


@pytest.mark.parametrize(
    ("code", "status", "address", "expected"),
    [
        (0, "NOERROR", "198.51.100.41", True),
        (None, "NOERROR", "198.51.100.41", False),
        (9, "NOERROR", "198.51.100.41", False),
        (0, "REFUSED", "198.51.100.41", False),
        (0, "NOERROR", "198.51.100.99", False),
        (0, "NOERROR", "", False),
    ],
)
def test_answer_matches(
    code: int | None, status: str, address: str, *, expected: bool
) -> None:
    query = smoke.queries(LAN, smoke.render_tailnet_zone(TEMPLATE))[0]
    output = reply(status, query.name.upper(), address)
    assert smoke.answer_matches(query, Result(code, output)) is expected
    assert not smoke.answer_matches(query, Result(stdout="not a DNS reply"))
    assert not smoke.answer_matches(
        query, Result(stdout=reply(status, "other", address))
    )
    # Additional owners cannot substitute for the expected answer; record order and
    # whitespace do not change the DNS RRset.
    output += "other. 60 IN A 198.51.100.99\n"
    assert smoke.answer_matches(query, Result(code, output)) is expected


@pytest.mark.parametrize(
    "name", ["ca.example.invalid", "otlp.example.invalid", "grafana.example.invalid"]
)
def test_aaaa_nodata_and_unexpected_answer(name: str) -> None:
    query = smoke.Query(
        "tailnet", "192.0.2", name, frozenset({"NOERROR"}), frozenset(), "AAAA"
    )
    assert smoke.dig_command(query)[-6] == "AAAA"
    assert smoke.answer_matches(query, Result(stdout=reply("NOERROR", name)))
    assert not smoke.answer_matches(query, Result(stdout=reply("REFUSED", name)))
    assert not smoke.answer_matches(query, Result(stdout=reply("NXDOMAIN", name)))
    assert not smoke.answer_matches(
        query,
        Result(stdout=reply("NOERROR", name) + f"{name}. 60 IN AAAA 2001:db8::1\n"),
    )


def test_staged_tree_modes(tmp_path: Path) -> None:
    class DiskFiles(Files):
        @override
        def mkdir(self, path: Path) -> None:
            path.mkdir(mode=0o700)

        @override
        def write_atomic(self, path: Path, text: str | bytes, mode: int) -> None:
            assert isinstance(text, str)
            path.write_text(text, encoding="utf-8")
            path.chmod(mode)

        @override
        def chmod(self, path: Path, mode: int) -> None:
            path.chmod(mode)

    tmp_path.chmod(0o700)
    smoke.SmokeProbe(System(filesystem=DiskFiles()), {}).prepare(SOURCE, tmp_path)
    assert {
        str(path.relative_to(tmp_path)): path.stat().st_mode & 0o777
        for path in (tmp_path, *tmp_path.rglob("*"))
    } == {
        ".": 0o755,
        "config": 0o755,
        "zones": 0o755,
        "config/Corefile": 0o644,
        "zones/lan.zone": 0o644,
        "zones/tailnet.zone": 0o644,
        "zones/ca.example.invalid.zone": 0o644,
        "zones/otlp.example.invalid.zone": 0o644,
        "zones/grafana.example.invalid.zone": 0o644,
    }


def test_prepare_and_start() -> None:
    system = System()
    probe = smoke.SmokeProbe(system, {"DOCKER_HOST": "unix:///dedicated"})
    image, command = probe.prepare(SOURCE, STAGE)
    fs = system.filesystem
    original = COREFILE
    assert fs.data[STAGE / "config/Corefile"].replace(" 203.0.113.2", "") == original
    assert fs.data[STAGE / "config/Corefile"].count(" 203.0.113.2") == COREFILE.count("bind ")
    assert fs.directories == [STAGE / "config", STAGE / "zones"]
    assert fs.modes == {
        STAGE: 0o755,
        STAGE / "config": 0o755,
        STAGE / "zones": 0o755,
    }
    assert fs.writes == [
        (STAGE / "config/Corefile", fs.data[STAGE / "config/Corefile"], 0o644),
        (STAGE / "zones/lan.zone", LAN, 0o644),
        (STAGE / "zones/tailnet.zone", smoke.render_tailnet_zone(TEMPLATE), 0o644),
        *[
            (
                STAGE / f"zones/{name}.zone",
                smoke.render_tailnet_zone(
                    ZONE_TEMPLATES[f"{name}.zone{suffix}"]
                ),
                0o644,
            )
            for name, suffix in (
                ("ca.example.invalid", ""),
                ("otlp.example.invalid", ".tmpl"),
                ("grafana.example.invalid", ".tmpl"),
            )
        ],
    ]
    probe.start(image, command)
    expected = [["pull", image], ["pull", DIG_PIN]]
    for view, subnet in (
        ("lan", "198.51.100"),
        ("tailnet", "192.0.2"),
        ("other", "203.0.113"),
    ):
        expected.append(
            (
                f"network create --driver bridge --subnet {subnet}.0/24 "
                f"dns-smoke-{view}"
            ).split()
        )
    expected += [
        command,
        [
            "network",
            "connect",
            "--ip",
            "192.0.2.2",
            "dns-smoke-tailnet",
            "dns-smoke-coredns",
        ],
        [
            "network",
            "connect",
            "--ip",
            "203.0.113.2",
            "dns-smoke-other",
            "dns-smoke-coredns",
        ],
        ["start", "dns-smoke-coredns"],
    ]
    assert system.runner.calls == [
        (["docker", *cmd], probe.env, 120) for cmd in expected
    ]
    assert probe.cleanup()
    assert [call[0] for call in system.runner.calls[-4:]] == [
        ["docker", "rm", "-f", "dns-smoke-coredns"],
        ["docker", "network", "rm", "dns-smoke-other"],
        ["docker", "network", "rm", "dns-smoke-tailnet"],
        ["docker", "network", "rm", "dns-smoke-lan"],
    ]


def test_docker_diagnostics_and_failure_message() -> None:
    system = System(runner=Runner(fail_at=1))
    probe = smoke.SmokeProbe(system, {"PATH": "/bin"})
    result = probe.docker(["inspect", "missing"], 7)
    assert result == Result(1, "failure output\n", "failure stderr\n")
    assert system.runner.calls == [
        (["docker", "inspect", "missing"], {"PATH": "/bin"}, 7)
    ]
    assert probe.diagnostics == [
        "$ docker inspect missing\nfailure output\nfailure stderr\n\nexit: 1\n"
    ]
    system.runner.fail_at = 2
    with pytest.raises(smoke.SmokeError) as failed:
        probe.require(["start", "missing"])
    assert str(failed.value) == "docker start missing failed"


def test_dispatch_success_retries_and_logs() -> None:
    system = System(runner=Runner(retry=True))
    out = io.StringIO()
    assert smoke.dispatch(args(), system, out) == 0
    assert system.sleeps == [1]
    assert out.getvalue() == SUCCESS_OUTPUT
    assert all(
        env == {"PATH": "/bin", "DOCKER_HOST": "unix:///run/dind/docker.sock"}
        for _, env, _ in system.runner.calls
    )
    assert {
        timeout for argv, _, timeout in system.runner.calls if argv[1] == "run"
    } == {15}
    assert Path("/artifacts") not in system.filesystem.directories


@pytest.mark.parametrize("failure", range(1, 67))
def test_dispatch_command_failures_unwind_and_save_diagnostics(failure: int) -> None:
    system = System(runner=Runner(fail_at=failure))
    out = io.StringIO()
    status = smoke.dispatch(args(), system, out)
    # First readiness query may fail once; logs cannot change a successful result.
    if failure in (10, 62):
        assert status == 0
        return
    assert status == 1
    assert "FAIL" in out.getvalue()
    fs = system.filesystem
    path, text, mode = fs.writes[-1]
    assert path == Path("/artifacts/commands.log")
    assert mode == 0o600
    assert isinstance(text, str)
    assert "$ docker " in text
    assert "failure output\nfailure stderr\n\nexit: 1\n" in text
    assert "server logs\nserver stderr" in text
    assert "exit: 0\n" in text
    assert fs.directories[-1] == Path("/artifacts")
    if failure >= 63:
        assert (
            out.getvalue()
            == SUCCESS_OUTPUT + "FAIL cleanup: smoke resources could not be removed\n"
        )
    if failure == 1:
        image, _ = smoke.service_command(COMPOSE, STAGE)
        assert text == (
            f"$ docker pull {image}\nfailure output\nfailure stderr\n\nexit: 1\n"
            "$ docker logs dns-smoke-coredns\nserver logs\nserver stderr\n\nexit: 0\n"
        )
    # All created resources are removed, even after one cleanup command fails.
    created = sum(call[0][1:3] == ["network", "create"] for call in system.runner.calls)
    removed = sum(call[0][1:3] == ["network", "rm"] for call in system.runner.calls)
    assert removed == created - (1 if failure in (3, 4, 5) else 0)


@pytest.mark.parametrize(
    "error",
    [
        OSError("filesystem failed"),
        ValueError("bad value"),
        KeyError("missing key"),
        TypeError("wrong type"),
        yaml.YAMLError("bad YAML"),
    ],
)
def test_dispatch_input_errors(error: Exception) -> None:
    system = System(filesystem=Files(error=error))
    out = io.StringIO()
    assert smoke.dispatch(args(), system, out) == 1
    assert out.getvalue() == f"FAIL dns smoke: {error}\n"
    assert system.runner.calls == [
        (
            ["docker", "logs", "dns-smoke-coredns"],
            {"PATH": "/bin", "DOCKER_HOST": "unix:///run/dind/docker.sock"},
            120,
        )
    ]


def test_readiness_timeout() -> None:
    system = System(runner=Runner(refuse_lan=True))
    probe = smoke.SmokeProbe(system, {})
    checks = smoke.queries(LAN, smoke.render_tailnet_zone(TEMPLATE))
    with pytest.raises(smoke.SmokeError) as timed_out:
        probe.check(checks, io.StringIO())
    assert str(timed_out.value) == "CoreDNS readiness timed out"
    assert system.clock == 30
    assert system.sleeps == [1] * 30
    # A failed check after readiness is distinct from a readiness timeout.
    query = replace(checks[0], answers=frozenset({"198.51.100.99"}))
    system.runner.refuse_lan = False
    with pytest.raises(smoke.SmokeError) as failed:
        probe.check((checks[0], query), io.StringIO())
    assert (
        str(failed.value)
        == "view lan: nas-a.s3.example.invalid failed; see commands.log"
    )
