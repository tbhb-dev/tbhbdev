"""Slice B classifiers, original checkpoint cases and export history fixtures."""

import base64
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from tbhbops.o11y import smoke_access as a
from tbhbops.o11y import smoke_export as e
from tbhbops.o11y import smoke_filter as f
lazy from tbhbops.o11y.validate import Document

EMPTY: Document = {}
EMPTY_LIST: list[object] = []


def packed(data: object) -> bytes:
    return json.dumps(data).encode()


def dashboards() -> dict[str, bytes]:
    return {
        "/api/search?type=dash-db": packed(
            [
                {"uid": uid, "folderUid": a.FOLDER if uid in a.AGENTS else ""}
                for uid in (*a.AGENTS, "o11y-github-activity", a.HEALTH)
            ]
        ),
        f"/api/folders/{a.FOLDER}": packed({"uid": a.FOLDER, "title": "Agents"}),
        **{
            f"/api/dashboards/uid/{uid}": packed(
                {
                    "dashboard": {"uid": uid},
                    "meta": {
                        "provisioned": False,
                        "provisionedExternalId": a.DASHBOARD_FILES[uid],
                        "folderUid": a.FOLDER if uid in a.AGENTS else "",
                    },
                }
            )
            for uid in (*a.AGENTS, "o11y-github-activity", a.HEALTH)
        },
    }


def test_ops_inventory() -> None:
    root = next(p for p in Path(__file__).parents if (p / "o11y").exists()) / "o11y"
    rules = yaml.safe_load(
        (root / "config/grafana/provisioning/alerting/rules.yaml").read_text()
    )
    assert tuple(a.RULES) == tuple(
        sorted(rule["uid"] for group in rules["groups"] for rule in group["rules"])
    )
    actual = {
        json.loads(p.read_text())["uid"]: p.name
        for p in (root / "config/grafana/dashboards").rglob("*.json")
    }
    assert set(actual) == {*a.AGENTS, a.HEALTH, "o11y-github-activity"}
    assert actual == a.DASHBOARD_FILES
    assert a.SOURCES == ("loki", "mimir", "tempo")
    assert a.OWNER == "owner@example.invalid"
    assert a.VIEWER == "viewer@example.invalid"


def test_folder_checkpoint_exact_membership() -> None:
    assert a.dashboard_checkpoint(dashboards())


@pytest.mark.parametrize("meta", [None, [], "malformed", 42, True])
def test_checkpoint_non_mapping_meta(meta: object) -> None:
    values = dashboards()
    uid = a.AGENTS[0]
    values[f"/api/dashboards/uid/{uid}"] = packed(
        {"dashboard": {"uid": uid}, "meta": meta}
    )
    assert not a.dashboard_checkpoint(values)


def test_checkpoint_attribute_error_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = a.decode

    class Meta:
        def __getitem__(self, _key: str) -> str:
            return "overview.json"

    def decode(body: bytes) -> Document:
        data = original(body)
        if isinstance(data, dict) and "meta" in data:
            data["meta"] = Meta()
        return data

    monkeypatch.setattr(a, "decode", decode)
    assert not a.dashboard_checkpoint(dashboards())


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "extra",
        "general",
        "leftover",
        "missing-capacity",
        "malformed",
    ],
)
def test_checkpoint_rejections(case: str) -> None:
    values = dashboards()
    search = json.loads(values["/api/search?type=dash-db"])
    if case == "duplicate":
        search.append(search[0])
    elif case == "extra":
        search.append({"uid": "extra", "folderUid": a.FOLDER})
    elif case in {"general", "leftover"}:
        search[0]["folderUid"] = ""
        if case == "leftover":
            search.append({"uid": "leftover", "folderUid": a.FOLDER})
    elif case == "missing-capacity":
        search.pop(1)
    else:
        search = None
    values["/api/search?type=dash-db"] = packed(search)
    assert not a.dashboard_checkpoint(values)
    assert not a.dashboard_checkpoint({})


@pytest.mark.parametrize("case", ["title", "folder-uid", "unavailable"])
def test_checkpoint_folder_rejections(case: str) -> None:
    values = dashboards()
    if case == "unavailable":
        values.pop(f"/api/folders/{a.FOLDER}")
    else:
        values[f"/api/folders/{a.FOLDER}"] = packed(
            {
                "uid": "wrong" if case == "folder-uid" else a.FOLDER,
                "title": "Other" if case == "title" else "Agents",
            }
        )
    assert not a.dashboard_checkpoint(values)


@pytest.mark.parametrize(
    "case", ["outside", "health", "capacity-source", "capacity-folder", "uid"]
)
def test_checkpoint_dashboard_rejections(case: str) -> None:
    values = dashboards()
    uid = {"outside": a.AGENTS[3], "health": a.HEALTH}.get(case, a.AGENTS[1])
    data = json.loads(values[f"/api/dashboards/uid/{uid}"])
    if case == "capacity-source":
        data["meta"]["provisionedExternalId"] = "wrong.json"
    elif case == "uid":
        data["dashboard"]["uid"] = "wrong"
    else:
        data["meta"]["folderUid"] = a.FOLDER if case == "health" else ""
    values[f"/api/dashboards/uid/{uid}"] = packed(data)
    assert not a.dashboard_checkpoint(values)


def test_general_checkpoint_is_out_of_scope() -> None:
    values = dashboards()
    for uid in a.AGENTS:
        data = json.loads(values[f"/api/dashboards/uid/{uid}"])
        data["meta"]["folderUid"] = ""
        values[f"/api/dashboards/uid/{uid}"] = packed(data)
    assert not a.dashboard_checkpoint(values)


def test_users_orgs_and_whitelist() -> None:
    assert a.user(packed({"login": "operator", "isGrafanaAdmin": False}), "operator")
    assert a.user(
        packed({"login": "admin", "isGrafanaAdmin": True}), "admin", admin=True
    )
    for data in (
        None,
        EMPTY,
        {"login": "wrong", "isGrafanaAdmin": False},
        {"login": "operator", "isGrafanaAdmin": 0},
    ):
        assert not a.user(packed(data), "operator")
    assert a.orgs(packed([{"orgId": 1, "role": "Admin"}]), "Admin")
    assert a.orgs(packed([{"role": "Admin"}]), "Admin", recovery=True)
    for data in (
        None,
        EMPTY_LIST,
        [None],
        [{"orgId": 2, "role": "Admin"}],
        [{"orgId": 1, "role": "Viewer"}],
        [{"orgId": 1, "role": "Admin"}] * 2,
    ):
        assert not a.orgs(packed(data), "Admin")
    assert a.whitelist(" 127.0.0.1\n")
    for text in ("", "127.0.0.1/32", "::1", "127.0.0.1\n127.0.0.2", "garbage"):
        assert not a.whitelist(text)


def owner() -> Document:
    return {
        "grafana-owner": {
            "user": "472:472",
            "depends_on": {"grafana": {"condition": "service_healthy"}},
            "volumes": [
                {"target": "/unrelated", "read_only": False},
                {"target": "/run/o11y/grafana_admin_password", "read_only": True},
            ],
        }
    }


def test_owner_model() -> None:
    assert a.owner_model(owner())
    for key in ("user", "depends_on", "volumes"):
        data = owner()
        del data["grafana-owner"][key]
        assert not a.owner_model(data)
    for key, value in (
        ("user", "0:0"),
        ("depends_on", {"grafana": {"condition": "service_started"}}),
        (
            "volumes",
            [{"target": "/run/o11y/grafana_admin_password", "read_only": False}],
        ),
    ):
        data = owner()
        data["grafana-owner"][key] = value
        assert not a.owner_model(data)
    broken: Document = {"grafana-owner": None}
    assert not a.owner_model(broken)


def points() -> list[Document]:
    return [
        {
            "uid": "o11y-discord",
            "name": "o11y",
            "provenance": "file",
            "type": "discord",
            "settings": {"url": "[REDACTED]", "use_discord_username": False},
        },
        {
            "uid": "o11y-email",
            "name": "o11y",
            "provenance": "file",
            "type": "email",
            "settings": {"addresses": "smoke-to@example.invalid", "singleEmail": True},
        },
    ]


def test_redaction_and_export() -> None:
    assert a.contact_points(packed(points()))
    for index in range(2):
        for key in ("uid", "name", "provenance", "type"):
            data = points()
            data[index][key] = "untrusted"
            assert not a.contact_points(packed(data))
        data = points()
        data[index]["settings"] = {"url": "exposed", "addresses": "wrong"}
        assert not a.contact_points(packed(data))
    for data in (None, EMPTY_LIST, [None, None], [EMPTY, EMPTY]):
        assert not a.contact_points(packed(data))
    assert a.decrypted(
        packed(
            {
                "contactPoints": [
                    {
                        "receivers": [
                            {"uid": "other"},
                            {"uid": "o11y-discord", "settings": {"url": "synthetic"}},
                        ]
                    }
                ]
            }
        ),
        "synthetic",
    )
    for data in (
        None,
        EMPTY,
        {
            "contactPoints": [
                {"receivers": [{"uid": "o11y-discord", "settings": {"url": "wrong"}}]}
            ]
        },
    ):
        assert not a.decrypted(packed(data), "synthetic")


def test_identifiers_home_and_logs() -> None:
    assert a.identifiers(
        packed([{"uid": "tempo"}, {"uid": "loki"}, {"uid": "mimir"}]), a.SOURCES
    )
    for data in (None, EMPTY, [None], [{"uid": "wrong"}], [{"uid": "loki"}] * 3):
        assert not a.identifiers(packed(data), a.SOURCES)
    assert a.home(packed({"homeDashboardUID": a.HEALTH}))
    assert a.home(packed({"redirectUri": "/d/o11y-health/stack-health"}), redirect=True)
    for data in (
        None,
        EMPTY,
        {"homeDashboardUID": "other"},
        {"redirectUri": 12},
        {"redirectUri": "/d/other/"},
    ):
        assert not a.home(packed(data))
        assert not a.home(packed(data), redirect=True)
    assert a.provisioning("level=error unrelated\nPrOvIsIoNiNg level=info") is None
    assert a.provisioning("level=error unrelated") == "4: provisioning-log-missing"
    for level in ("warn", "ERROR"):
        assert (
            a.provisioning(f"provisioning level={level} private-body")
            == "4: provisioning-log-error"
        )


def filtered(service: str, payload: bytes) -> Document:
    data = json.loads(payload)
    spans = data["resourceSpans"][0]["scopeSpans"][0]["spans"]
    if service == "codex_exec":
        spans[:] = [spans[i] for i in (0, 7, 8, 9)]
    elif service == "codex_tui":
        spans[:] = spans[:1]
    return {"batches": data["resourceSpans"]}


@pytest.mark.parametrize("service", ["codex_exec", "codex_tui", "o11y-check-fixture"])
def test_filter_fixture_identities(service: str) -> None:
    payload = f.fixture(service, "0123456789abcdef" * 2, 1700000000)
    data = json.loads(payload)
    batch = data["resourceSpans"][0]
    assert batch["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": service}}
    ]
    assert batch["scopeSpans"][0]["scope"] == {"name": "o11y-filter-check"}
    spans = batch["scopeSpans"][0]["spans"]
    assert [s["name"] for s in spans[:4]] == [
        "turn/start",
        "receiving",
        "handle_responses",
        "persist_rollout_items",
    ]
    assert [s["name"] for s in spans[4:7]] == [
        "realtime_conversation.running_state",
        "fs.get_metadata",
        "fs.read_file",
    ]
    assert [s["name"] for s in spans[7:]] == (
        ["tool/exec", "request/send", "child/retained"]
        if service == "codex_exec"
        else []
    )
    for i, span in enumerate(spans):
        assert span == {
            "traceId": "0123456789abcdef" * 2,
            "spanId": format(i + 1, "016x"),
            "name": span["name"],
            "kind": 1,
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano": "1700000000500000000",
            "attributes": [
                {"key": "fixture.name", "value": {"stringValue": span["name"]}},
                {"key": "fixture.index", "value": {"intValue": str(i)}},
                {"key": "fixture.kept", "value": {"boolValue": True}},
            ],
            **(
                {"parentSpanId": "0000000000000003" if i == 9 else "0000000000000001"}
                if i
                else {}
            ),
        }


@pytest.mark.parametrize("service", ["codex_exec", "codex_tui", "o11y-check-fixture"])
def test_filter_mutations(service: str) -> None:
    payload = f.fixture(service, "0123456789abcdef" * 2, 1700000000)
    actual = filtered(service, payload)
    assert f.found(packed(actual), service, payload)
    for mutation in (
        "missing",
        "extra",
        "name",
        "spanId",
        "parentSpanId",
        "attributes",
        "service",
    ):
        broken = copy.deepcopy(actual)
        target = broken["batches"][0]["scopeSpans"][0]["spans"]
        if mutation == "missing":
            target.pop(0)
        elif mutation == "extra":
            target.append(target[0])
        elif mutation == "attributes":
            target[0]["attributes"][0]["value"]["stringValue"] = "changed"
        elif mutation == "service":
            broken["batches"][0]["resource"]["attributes"][0]["value"][
                "stringValue"
            ] = "other"
        else:
            target[0][mutation] = "000000000000000f"
        assert not f.found(packed(broken), service, payload)


@pytest.mark.parametrize("service", ["codex_exec", "codex_tui", "o11y-check-fixture"])
def test_filter_normalized_identities(service: str) -> None:
    payload = f.fixture(service, "0123456789abcdef" * 2, 1700000000)
    actual = filtered(service, payload)
    for span in actual["batches"][0]["scopeSpans"][0]["spans"]:
        for key in ("spanId", "parentSpanId"):
            if key in span:
                span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
        span["attributes"].reverse()
    actual["batches"][0]["scopeSpans"][0]["spans"].reverse()
    assert f.found(packed(actual), service, payload)
    assert not f.found(b"null", service, payload)
    assert not f.found(b"{}", service, payload)
    assert not f.found(packed(actual), service, b"null")
    assert (
        f.span_id(base64.b64encode(bytes.fromhex("0000000000000001")).decode())
        == "0000000000000001"
    )
    with pytest.raises(ValueError, match="base64"):
        f.span_id("invalid!")


def test_filter_attribute_error_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    class Span:
        def __getitem__(self, key: str) -> str:
            return {"spanId": "0000000000000001", "name": "root"}[key]

    payload = f.fixture("control", "0123456789abcdef" * 2, 1700000000)
    actual = filtered("control", payload)
    actual["batches"][0]["scopeSpans"][0]["spans"] = [Span()]
    original = f.decode

    def decode(body: bytes) -> Document:
        return actual if body == b"response" else original(body)

    monkeypatch.setattr(f, "decode", decode)
    assert not f.found(b"response", "control", payload)


def test_filter_wiring() -> None:
    root = next(p for p in Path(__file__).parents if (p / "o11y").exists())
    config = (root / "o11y/config/alloy/config.alloy").read_text()
    transform = config.split('otelcol.processor.transform "resource" {')[1].split(
        "\n}"
    )[0]
    assert "traces  = [otelcol.processor.filter.codex.input]" in transform
    for signal in ("metrics", "logs"):
        assert f"{signal}" in transform
        assert re.search(
            rf"{signal}\s*= \[otelcol.processor.batch.default.input\]", transform
        )
    block = config.split('otelcol.processor.filter "codex" {')[1].split("\n}")[0]
    assert 'IsMatch(resource.attributes["service.name"], "^codex") and (' in block
    assert "traces = [otelcol.processor.batch.default.input]" in block
    for name in f.DROPPED:
        assert config.count(f'name == "{name}"') == 1
    assert config.count("otelcol.processor.filter.codex.input") == 1
    assert not re.search(r"\b(metrics|logs)\s*[={]", block)
    assert re.findall(r'name == "([^"]+)"', block) == list(f.DROPPED)
    assert re.findall(r"// ([0-9,]+)$", block, re.MULTILINE) == [
        "2,061",
        "2,061",
        "750",
        "683",
        "495",
        "122",
    ]
    assert '], " or ") + `)`]' in block
    assert "example-org/o11y-example#40" in block


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        *[
            packed({parent: {child: value}})
            for parent, child in (
                ("partialSuccess", "rejectedDataPoints"),
                ("partial_success", "rejected_data_points"),
            )
            for value in (None, 0, 0.0, "0")
        ],
        b'{"partialSuccess":{}}',
        b'{"partial_success":{}}',
    ],
)
def test_upload_accepts_zero(body: bytes) -> None:
    assert e.upload(body)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"invalid",
        b"null",
        b"[]",
        b"\xff",
        *[
            packed({parent: {child: value}})
            for parent, child in (
                ("partialSuccess", "rejectedDataPoints"),
                ("partial_success", "rejected_data_points"),
            )
            for value in (1, "7", False, True)
        ],
        b'{"partialSuccess":{"rejectedDataPoints":0},"partial_success":{"rejected_data_points":1}}',
        b'{"partialSuccess":{"rejectedDataPoints":1},"partial_success":{"rejected_data_points":0}}',
        b'{"partialSuccess":null}',
    ],
)
def test_upload_rejects_partial_or_invalid(body: bytes) -> None:
    assert not e.upload(body)


def test_histories_and_cutoff() -> None:
    data = json.loads(e.histories(10000))
    resource = data["resourceMetrics"][0]
    assert resource["resource"] == {
        "attributes": [{"key": "service.name", "value": {"stringValue": "o11y/alloy"}}]
    }
    scope = resource["scopeMetrics"][0]
    assert scope["scope"] == {"name": "o11y-smoke-export"}
    metrics = scope["metrics"]
    assert len(metrics) == 49
    assert e.STEP == 30
    assert tuple(e.COUNTERS) == (
        *(
            f"otelcol_exporter_{action}_failed_{signal}_total"
            for action in ("send", "enqueue")
            for signal in ("spans", "metric_points", "log_records")
        ),
        "prometheus_remote_storage_samples_failed_total",
    )
    cases = (
        "burst",
        "boundary",
        "old-burst",
        "reset",
        "healed",
        "new-counter",
        "ongoing",
    )
    assert tuple(e.CASES) == cases


@pytest.mark.parametrize(
    ("case", "index", "last"),
    [
        ("burst", 0, "10"),
        ("boundary", 1, "10"),
        ("old-burst", 2, "10"),
        ("reset", 3, "0"),
        ("healed", 4, "1200"),
        ("new-counter", 5, "10"),
        ("ongoing", 6, "1800"),
    ],
)
def test_history_points(case: str, index: int, last: str) -> None:
    data = json.loads(e.histories(10000))
    metrics = data["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    for i in range(index, len(metrics), 7):
        metric = metrics[i]
        assert metric["name"] == e.COUNTERS[i // 7]
        assert metric["sum"]["aggregationTemporality"] == 2
        assert metric["sum"]["isMonotonic"] is True
        points = metric["sum"]["dataPoints"]
        assert [int(p["timeUnixNano"]) // 1000000000 for p in points] == list(
            range(9760 if case == "new-counter" else 8800, 10001, 30)
        )
        assert all(int(p["timeUnixNano"]) % 1000000000 == 0 for p in points)
        for point in points:
            assert point["startTimeUnixNano"] == "8170000000000"
            assert point["attributes"] == [
                {
                    "key": "component_id",
                    "value": {"stringValue": f"smoke-{i // 7}-{case}"},
                }
            ]
        assert points[-1]["asInt"] == last


def test_counter_cutoff() -> None:
    for case, ticks in {
        "burst": (1739, 1740),
        "boundary": (1499, 1500),
        "old-burst": (1199, 1200),
        "reset": (1349, 1350),
        "healed": (1199, 1200, 1201),
    }.items():
        assert [e.counter(case, t) for t in ticks] == (
            {
                "burst": [0, 10],
                "boundary": [0, 10],
                "old-burst": [0, 10],
                "reset": [10, 0],
                "healed": [1199, 1200, 1200],
            }[case]
        )


def test_production_rule() -> None:
    for duration, pending in (
        ("2m", 120),
        ("120s", 120),
        ("10m", 600),
        ("30s", 30),
        ("0s", 0),
        ("601s", 601),
        ("1s", 1),
        ("11m", 660),
    ):
        assert e.production_rule(
            packed(
                {
                    "for": duration,
                    "data": [
                        {"refId": "B"},
                        {"refId": "A", "model": {"expr": "production"}},
                    ],
                }
            )
        ) == ("production", pending)
    for duration in ("1h", "2m\n"):
        assert (
            e.production_rule(
                packed(
                    {"for": duration, "data": [{"refId": "A", "model": {"expr": "x"}}]}
                )
            )
            is None
        )
    for queries in (
        EMPTY_LIST,
        [{"refId": "A", "model": {"expr": ""}}],
        [{"refId": "A", "model": {"expr": 1}}],
        [{"refId": "A", "model": {"expr": "x"}}] * 2,
    ):
        assert e.production_rule(packed({"for": "2m", "data": queries})) is None
    invalid_for: Document = {"for": None}
    for data in (None, EMPTY, invalid_for):
        assert e.production_rule(packed(data)) is None


def sustained() -> Document:
    return {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"component_id": f"smoke-{i}-ongoing"},
                    "values": [[t, "1"] for t in range(5)],
                }
                for i in range(7)
            ]
        },
    }


def test_export_arrival() -> None:
    assert e.arrived(b'{"data":{"result":[{"value":[0,"49"]}]}}')
    for data in (
        None,
        EMPTY,
        {"data": {"result": EMPTY_LIST}},
        {"data": {"result": [{"value": [0, "48"]}]}},
    ):
        assert not e.arrived(packed(data))


def test_export_results() -> None:
    assert e.sustained(packed(sustained()), 120)
    for case in (
        "status",
        "extra",
        "missing",
        "samples",
        "zero",
        "bad",
        "shape",
        "label",
    ):
        data = sustained()
        if case == "status":
            data["status"] = "error"
        elif case == "extra":
            data["data"]["result"].append(data["data"]["result"][0])
        elif case == "missing":
            data["data"]["result"].pop()
        elif case == "label":
            data["data"]["result"][0]["metric"]["component_id"] = None
        else:
            data["data"]["result"][0]["values"] = (
                []
                if case == "samples"
                else [[0, "0" if case == "zero" else "bad"]] * 5
                if case != "shape"
                else [[]] * 5
            )
        assert not e.sustained(packed(data), 120)
    assert not e.sustained(b"null", 120)
    assert not e.sustained(b"{}", 120)


def test_export_objects() -> None:
    for text in ("1", " 2\n", "01"):
        assert e.objects(text)
    for text in ("0", "-1", "1.0", "body", "", "1\n1"):
        assert not e.objects(text)


def test_span_normalization_contract() -> None:
    assert f.span_id("0123456789abcdef") == "0123456789abcdef"
    with pytest.raises(ValueError, match="base64"):
        f.span_id(base64.b64encode(bytes.fromhex("0000000000000001")).decode() + "!")
    root = {
        "spanId": "0000000000000001",
        "name": "root",
        "attributes": [{"key": "z"}, {"key": "a"}],
    }
    assert f.normalized(root) == {
        "spanId": "0000000000000001",
        "name": "root",
        "parentSpanId": "",
        "attributes": [{"key": "a"}, {"key": "z"}],
    }
    root["parentSpanId"] = base64.b64encode(bytes.fromhex("0000000000000002")).decode()
    assert f.normalized(root)["parentSpanId"] == "0000000000000002"
    assert not e.sustained(packed(sustained()), 121)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        (0, True),
        (0.0, True),
        ("0", True),
        (False, False),
        (True, False),
        (1, False),
        ("1", False),
    ],
)
def test_zero_rejected_count(value: object, expected: bool) -> None:
    assert e.zero_rejected(value) is expected
