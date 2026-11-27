"""Check the deployment role's public inputs and rendered Compose wiring."""

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import cast
from unittest.mock import patch

import jinja2
import pytest
import yaml

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "roles").is_dir()
)
ROLE = ANSIBLE / "roles" / "o11y"


def source(relative: str) -> str:
    return (ROLE / relative).read_text()


@pytest.fixture
def settings() -> dict[str, object]:
    return cast("dict[str, object]", yaml.safe_load(source("defaults/main.yml"))) | {
        "o11y_root": "/srv/o11y",
        "o11y_data_dir": "/srv/o11y-data",
        "o11y_release_id": "a" * 40,
        "o11y_host_name": "observability",
        "o11y_grafana_root_url": "https://grafana.example.invalid/",
        "o11y_grafana_owner_login": "owner@example.invalid",
        "o11y_alert_email_to": "to@example.invalid",
        "o11y_alert_smtp_from": "from@example.invalid",
        "o11y_alert_smtp_host": "smtp.example.invalid",
        "o11y_alert_smtp_port": 587,
        "o11y_alert_smtp_user": "user@example.invalid",
        "o11y_configs": {
            "files": [
                {"path": "/scratch/" + name + "-123456789abc"}
                for name in ("alloy", "buckets", "grafana", "loki", "mimir", "tempo")
            ],
        },
    }


def render(name: str, settings: dict[str, object]) -> str:
    def basename(path: str) -> str:
        return Path(path).name

    environment = jinja2.Environment(  # noqa: S701 - Compose and environment text
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
    )
    cast("dict[str, object]", environment.filters).update(
        to_json=json.dumps,
        from_json=json.loads,
        basename=basename,
    )

    def lookup(_plugin: str, template: str) -> str:
        return environment.get_template(template).render(settings)

    cast("dict[str, object]", environment.globals)["lookup"] = lookup
    return environment.get_template(name).render(settings)


def test_required_inputs_have_no_defaults_and_spec_defaults_match() -> None:
    defaults = yaml.safe_load(source("defaults/main.yml"))
    specs = yaml.safe_load(source("meta/argument_specs.yml"))["argument_specs"]
    options = specs["main"]["options"]
    required = {
        "root",
        "data_dir",
        "host_name",
        "grafana_root_url",
        "grafana_owner_login",
        "alert_email_to",
        "alert_smtp_from",
        "alert_smtp_host",
        "alert_smtp_port",
        "alert_smtp_user",
        "alert_discord_webhook_ref",
        "alert_smtp_password_ref",
        "secrets_vault",
        "lookup_command",
        "release_id",
    }
    assert {name for name, spec in options.items() if spec.get("required")} == {
        "o11y_" + name for name in required
    }
    assert {
        name: spec["default"] for name, spec in options.items() if "default" in spec
    } == defaults
    assert set(options) == set(defaults) | {"o11y_" + name for name in required}
    assert set(specs["rollback"]["options"]) == {"o11y_root", *defaults}


def test_environment_and_config_paths_match_compose(
    settings: dict[str, object],
) -> None:
    env = dict(
        line.split("=", 1) for line in render("compose.env.j2", settings).splitlines()
    )
    assert json.loads(env["O11Y_S3_ENV_FILE"]) == "/srv/o11y-data/secrets/s3.env"
    assert json.loads(env["O11Y_DATA_DIR"]) == "/srv/o11y-data"
    assert env["O11Y_ALERT_SMTP_ENABLED"] == "true"
    assert env["O11Y_ALERT_SMTP_SKIP_VERIFY"] == "false"
    for service in ("alloy", "buckets", "grafana", "loki", "mimir", "tempo"):
        assert json.loads(env["O11Y_CONFIG_" + service.upper()]) == (
            "/srv/o11y/releases/" + "a" * 40 + "/config/" + service + "-123456789abc"
        )
    assert not any("PASSWORD" in key or "WEBHOOK" in key for key in env)
    environment = json.loads(render("environment.json.j2", settings))
    assert environment["O11Y_GRAFANA_OWNER_LOGIN"] == "owner@example.invalid"
    assert environment["O11Y_DISK_RESERVE_GB"] == "50"


def test_role_uses_only_the_original_compose_ports(settings: dict[str, object]) -> None:
    assert not (ROLE / "templates/compose.override.yaml.j2").exists()
    assert not (ROLE / "templates/health.json.j2").exists()
    environment = json.loads(render("environment.json.j2", settings))
    assert not any(key.endswith("_PORT") and "SMTP" not in key for key in environment)
    tasks = source("tasks/render.yml") + source("tasks/stage.yml")
    assert "compose.override" not in tasks
    assert "health.json" not in tasks


def test_secret_delivery_has_no_retained_values_or_diffs() -> None:
    tasks = yaml.safe_load(source("tasks/release.yml"))
    init = tasks[2]
    delivery = tasks[3]
    for task in (init, delivery):
        assert task["no_log"] is True
        assert task["diff"] is False
        assert "register" not in task
    copy = delivery["ansible.builtin.copy"]
    assert "lookup('ansible.builtin.pipe'" in copy["content"]
    assert "quote" in copy["content"]
    assert copy["mode"] == "0400"
    assert copy["owner"] == copy["group"] == "472"


def test_lock_is_atomic_and_cleanup_cannot_remove_a_competing_lock() -> None:
    acquire, refusal, transaction = yaml.safe_load(source("tasks/locked.yml"))
    assert acquire["ansible.builtin.command"]["argv"] == [
        "mkdir",
        "{{ o11y_root }}/deploy.lock",
    ]
    message = refusal["ansible.builtin.assert"]["fail_msg"]
    assert "{{ o11y_root }}/deploy.lock" in message
    assert "by hand after checking that no run is active" in message
    assert transaction["always"][0]["ansible.builtin.file"]["state"] == "absent"
    assert len(transaction["block"]) == 1


@pytest.mark.parametrize("playbook", ["o11y.yml", "o11y-rollback.yml"])
def test_playbooks_target_only_o11y_one_at_a_time(playbook: str) -> None:
    play = yaml.safe_load((ANSIBLE / "playbooks" / playbook).read_text())[0]
    assert play["hosts"] == "o11y"
    assert play["serial"] == 1
    assert play["become"] is True


@pytest.mark.parametrize(
    ("root", "mounts", "expected"),
    [
        ("/srv/o11y", ["/", "/srv", "/srv/o11y"], "/srv/o11y"),
        ("/srv/o11y", ["/", "/srv/o11y-other"], "/"),
        ("/srv/o11y", ["/srv", "/", "/srv/other"], "/srv"),
    ],
)
def test_preflight_selects_the_containing_filesystem(
    root: str, mounts: list[str], expected: str
) -> None:
    condition = yaml.safe_load(source("tasks/preflight.yml"))[2]["when"]
    environment = jinja2.Environment()  # noqa: S701 - boolean expressions only
    evaluate = environment.compile_expression(condition)
    selected = [
        mount
        for mount in sorted(mounts)
        if evaluate(item={"mount": mount}, o11y_root=root)
    ]
    assert selected[-1] == expected


def test_completed_releases_refuse_changed_inputs() -> None:
    stage = yaml.safe_load(source("tasks/stage.yml"))
    refusal = stage[3]["ansible.builtin.assert"]
    assert "o11y_input_digest" in refusal["that"]
    assert "new ops commit" in refusal["fail_msg"]
    assert stage[4]["when"] == "not o11y_sealed.stat.exists"
    assert stage[5]["ansible.builtin.command"]["argv"] == [
        "docker",
        "compose",
        "config",
        "-q",
    ]
    assert stage[5]["no_log"] is True


@pytest.mark.parametrize("cert_dir", [None, ""])
def test_tls_off_redeploy_matches_main_environment_and_digest(
    settings: dict[str, object], cert_dir: str | None
) -> None:
    # Snapshot of main at 4ee14465, before the TLS input was introduced.
    baseline = Path(__file__).parent / "testdata/o11y/environment-main.json.j2"
    environment = jinja2.Environment(trim_blocks=True)  # noqa: S701 - JSON text
    environment.filters["to_json"] = json.dumps
    original = environment.from_string(baseline.read_text()).render(settings)
    settings.pop("o11y_grafana_tls_cert_dir")
    if cert_dir is not None:
        settings["o11y_grafana_tls_cert_dir"] = cert_dir
    actual = render("environment.json.j2", settings)
    assert actual.encode() == original.encode()
    digest = hashlib.sha256(original.encode()).hexdigest()
    actual_digest = hashlib.sha256(actual.encode()).hexdigest()
    assert actual_digest == digest
    stage = yaml.safe_load(source("tasks/stage.yml"))

    def b64decode(value: str) -> str:
        return base64.b64decode(value).decode()

    environment.filters["b64decode"] = b64decode
    assert environment.compile_expression(stage[3]["ansible.builtin.assert"]["that"])(
        o11y_seal={"content": base64.b64encode((digest + "\n").encode()).decode()},
        o11y_input_digest=actual_digest,
    )


def test_switch_recovers_health_before_restoring_current_and_still_fails() -> None:
    switch = yaml.safe_load(source("tasks/switch.yml"))[-1]
    rescue = switch["rescue"]
    stop = next(task for task in rescue if "ansible.builtin.command" in task)
    assert stop["failed_when"] is False
    recovery = next(task for task in rescue if "block" in task)
    assert recovery["when"] == "o11y_old | length > 0"
    assert recovery["block"][0]["ansible.builtin.set_fact"] == {
        "o11y_target": "{{ o11y_old }}"
    }
    assert recovery["block"][1]["ansible.builtin.include_tasks"] == "up.yml"
    assert (
        recovery["block"][2]["ansible.builtin.file"]["dest"]
        == "{{ o11y_root }}/current"
    )
    assert recovery["rescue"][0]["ansible.builtin.fail"]
    assert "No previous release exists" in rescue[-1]["ansible.builtin.fail"]["msg"]
    assert "ansible.builtin.fail" in rescue[-1]
    assert (
        yaml.safe_load(source("tasks/up.yml"))[2]["ansible.builtin.include_tasks"]
        == "health.yml"
    )


def test_rollback_checks_the_selected_previous_release() -> None:
    rollback = yaml.safe_load(source("tasks/rollback.yml"))
    assert rollback[1]["vars"]["o11y_action"] == "previous.yml"
    assert rollback[1]["ansible.builtin.include_tasks"] == "locked.yml"
    transaction = yaml.safe_load(source("tasks/locked.yml"))[-1]
    assert transaction["block"][0]["ansible.builtin.include_tasks"] == (
        "{{ o11y_action }}"
    )
    previous = yaml.safe_load(source("tasks/previous.yml"))
    assert previous[-2]["ansible.builtin.set_fact"] == {
        "o11y_target": "{{ o11y_previous.stat.lnk_source }}"
    }
    assert previous[-1]["ansible.builtin.include_tasks"] == "switch.yml"
    switch = yaml.safe_load(source("tasks/switch.yml"))[-1]
    assert switch["block"][1]["ansible.builtin.include_tasks"] == "up.yml"


def difference(left: list[str], right: list[str]) -> list[str]:
    return list(set(left) - set(right))


def intersect(left: list[str], right: list[str]) -> list[str]:
    return sorted(set(left) & set(right))


@pytest.mark.parametrize("release_tls", [False, True])
@pytest.mark.parametrize("cert_dir", [None, "", "/srv/o11y/tls"])
def test_health_expected_services_come_from_target_release(
    release_tls: bool, cert_dir: str | None
) -> None:
    read, gate, _probe = yaml.safe_load(source("tasks/health.yml"))
    assert read["ansible.builtin.command"] == {
        "argv": ["docker", "compose", "config", "--services"],
        "chdir": "{{ o11y_target }}",
    }
    assert read["register"] == "o11y_release_services"
    assert read["changed_when"] is False
    assert gate["ansible.builtin.command"]["chdir"] == "{{ o11y_target }}"
    services = ["grafana", "rustfs", "loki", "tempo", "mimir", "alloy"]
    released = services + (["grafana-tls"] if release_tls else []) + ["ts", "buckets"]
    inputs: dict[str, object] = {"o11y_release_services": {"stdout_lines": released}}
    if cert_dir is not None:
        inputs["o11y_grafana_tls_cert_dir"] = cert_dir
    environment = jinja2.Environment(  # noqa: S701 - service names only
        undefined=jinja2.StrictUndefined
    )
    environment.filters["intersect"] = intersect
    expected = services + (["grafana-tls"] if release_tls else [])
    expression = gate["vars"]["o11y_expected_services"]
    assert set(
        yaml.safe_load(environment.from_string(expression).render(inputs))
    ) == set(expected)


@pytest.mark.parametrize(
    "target",
    [
        (service, cert_dir)
        for service in ("tempo", "grafana-tls")
        for cert_dir in ("", "/srv/o11y/tls")
    ],
)
@pytest.mark.parametrize(
    ("state", "health", "missing", "expected"),
    [
        ("running", "healthy", False, True),
        ("running", "", False, True),
        ("running", "starting", False, False),
        ("running", "unhealthy", False, False),
        ("exited", "healthy", False, False),
        ("running", "healthy", True, False),
    ],
)
def test_container_gate_requires_every_expected_service(
    target: tuple[str, str], state: str, health: str, missing: bool, *, expected: bool
) -> None:
    service, cert_dir = target
    task = next(
        task
        for task in yaml.safe_load(source("tasks/health.yml"))
        if task.get("register") == "o11y_health"
    )
    environment = jinja2.Environment()  # noqa: S701 - boolean expressions only
    environment.filters["intersect"] = intersect
    services = yaml.safe_load(
        environment.from_string(task["vars"]["o11y_expected_services"]).render(
            o11y_grafana_tls_cert_dir=cert_dir,
            o11y_release_services={
                "stdout_lines": [
                    "grafana",
                    "rustfs",
                    "loki",
                    "tempo",
                    "mimir",
                    "alloy",
                    "grafana-tls",
                ]
            },
        )
    )
    assert "grafana-tls" in services
    containers = [
        {"Service": service, "State": "running", "Health": "healthy"}
        for service in services
    ]
    selected = next(
        container for container in containers if container["Service"] == service
    )
    selected.update(State=state, Health=health)
    if missing:
        containers.remove(selected)
    with patch.dict(environment.filters, difference=difference):
        assert (
            all(
                environment.compile_expression(condition)(
                    o11y_expected_services=services,
                    o11y_long_running=containers,
                )
                for condition in task["until"]
            )
            is expected
        )


def test_exec_probes_match_the_pinned_compose_health_checks() -> None:
    compose = yaml.safe_load((ANSIBLE.parent / "o11y/compose.yaml").read_text())
    probe = next(
        task
        for task in yaml.safe_load(source("tasks/health.yml"))
        if task.get("register") == "o11y_probe"
    )
    assert "'exec', '-T', item.service" in probe["ansible.builtin.command"]["argv"]
    assert "Health | length == 0" in probe["when"]
    for item in probe["loop"]:
        command = compose["services"][item["service"]]["healthcheck"]["test"][1:]
        assert item["probe"] == [part.replace("$$", "$") for part in command]


def test_startup_errors_still_run_the_gate_and_cannot_pass_deployment() -> None:
    _tls, start, gate, result = yaml.safe_load(source("tasks/up.yml"))
    assert start["failed_when"] is False
    assert gate["ansible.builtin.include_tasks"] == "health.yml"
    assert result["ansible.builtin.assert"]["that"] == "o11y_up.rc == 0"


@pytest.mark.parametrize(
    ("name", "value", "valid"),
    json.loads(
        (Path(__file__).parent / "testdata/o11y/network-inputs.json").read_text()
    ),
)
def test_network_role_inputs(
    settings: dict[str, object], name: str, value: str, *, valid: bool
) -> None:
    variable = "o11y_" + name
    expressions = yaml.safe_load(source("tasks/main.yml"))[0]["ansible.builtin.assert"][
        "that"
    ]
    expression = next(e for e in expressions if e.startswith(variable + " is match"))
    pattern = expression.split("match('")[1].removesuffix("')")
    assert bool(re.match(pattern, value)) == valid
    settings[variable] = value
    environment = json.loads(render("environment.json.j2", settings))
    lines = render("compose.env.j2", settings).splitlines()
    key = "O11Y_" + name.upper()
    if name == "grafana_tls_cert_dir" and not value:
        assert key not in environment
        assert not any(line.startswith(key + "=") for line in lines)
    else:
        assert environment[key] == value
        assert key + "=" + json.dumps(value) in lines


def test_tls_config_path(settings: dict[str, object]) -> None:
    settings["o11y_configs"] = {
        "files": [{"path": "/scratch/grafana_tls-123456789abc"}]
    }
    lines = render("compose.env.j2", settings).splitlines()
    expected = "/srv/o11y/releases/" + "a" * 40 + "/config/grafana_tls-123456789abc"
    assert "O11Y_CONFIG_GRAFANA_TLS=" + json.dumps(expected) in lines


@pytest.mark.parametrize("path", ["rollback", "recovery"])
@pytest.mark.parametrize("release_tls", [False, True])
@pytest.mark.parametrize("cert_dir", ["", "/inventory/missing-tls"])
def test_tls_validation_uses_target_on_rollback_and_recovery(
    path: str, release_tls: bool, cert_dir: str
) -> None:
    previous = yaml.safe_load(source("tasks/previous.yml"))
    switch = yaml.safe_load(source("tasks/switch.yml"))[-1]
    recovery = next(task for task in switch["rescue"] if "block" in task)["block"]
    selection, start = previous[-2:] if path == "rollback" else recovery[:2]
    target = "/srv/o11y/releases/previous"
    environment = jinja2.Environment()  # noqa: S701 - paths and boolean expressions
    environment.filters["from_json"] = json.loads
    assert (
        environment.from_string(
            selection["ansible.builtin.set_fact"]["o11y_target"]
        ).render(o11y_previous={"stat": {"lnk_source": target}}, o11y_old=target)
        == target
    )
    assert start["ansible.builtin.include_tasks"] == (
        "switch.yml" if path == "rollback" else "up.yml"
    )
    # Evaluate the actual certificate gate with missing files. A legacy release
    # must start even when today's inventory enables TLS at a missing path.
    assert "o11y_grafana_tls_cert_dir" not in source("tasks/preflight.yml")
    read, block = yaml.safe_load(source("tasks/tls.yml"))
    assert read["ansible.builtin.command"]["chdir"] == "{{ o11y_target }}"
    assert read["no_log"] is True
    up = yaml.safe_load(source("tasks/up.yml"))
    assert up[0]["ansible.builtin.include_tasks"] == "tls.yml"
    services: dict[str, object] = {"grafana": {}}
    if release_tls:
        services["grafana-tls"] = {
            "volumes": [{"target": "/tls", "source": "/release/missing-tls"}]
        }
    inputs: dict[str, object] = {
        "o11y_grafana_tls_cert_dir": cert_dir,
        "o11y_target_compose": {"stdout": json.dumps({"services": services})},
    }
    enabled = environment.compile_expression(block["when"])(**inputs)
    assert enabled == release_tls
    if enabled:
        inputs.update(
            {
                key: environment.from_string(value).render(inputs)
                for key, value in block.get("vars", {}).items()
            }
        )
        probe, require = block["block"]
        assert (
            environment.from_string(probe["ansible.builtin.stat"]["path"]).render(
                **inputs, item="grafana.crt.pem"
            )
            == "/release/missing-tls/grafana.crt.pem"
        )
        assert not all(
            environment.compile_expression(condition)(item={"stat": {"exists": False}})
            for condition in require["ansible.builtin.assert"]["that"]
        )


@pytest.mark.parametrize("cert_dir", ["", "/srv/o11y/tls"])
@pytest.mark.parametrize("filename", ["grafana.crt.pem", "grafana.key.pem"])
@pytest.mark.parametrize(
    "stat",
    [
        {"exists": False},
        {"exists": True, "isreg": False},
        {"exists": True, "isreg": True},
    ],
)
def test_tls_preflight(cert_dir: str, filename: str, stat: dict[str, bool]) -> None:
    read, block = yaml.safe_load(source("tasks/tls.yml"))
    assert read["ansible.builtin.command"]["argv"] == [
        "docker",
        "compose",
        "config",
        "--format",
        "json",
        "--no-env-resolution",
    ]
    environment = jinja2.Environment()  # noqa: S701 - paths and boolean expressions
    environment.filters["from_json"] = json.loads
    assert environment.compile_expression(block["when"])(
        o11y_target_compose={
            "stdout": json.dumps({"services": {"grafana-tls": {}} if cert_dir else {}})
        }
    ) == bool(cert_dir)
    probe, require = block["block"]
    assert probe["loop"] == ["grafana.crt.pem", "grafana.key.pem"]
    assert probe["ansible.builtin.stat"] == {
        "path": "{{ o11y_target_tls_cert_dir }}/{{ item }}",
        "follow": True,
        "get_checksum": False,
        "get_mime": False,
    }
    assert probe["register"] == "o11y_grafana_tls_files"
    assert require["loop"] == "{{ o11y_grafana_tls_files.results }}"
    item = {"item": filename, "stat": stat}
    assertion = require["ansible.builtin.assert"]
    assert all(
        environment.compile_expression(c)(item=item) for c in assertion["that"]
    ) == (stat.get("isreg", False))
    message = environment.from_string(assertion["fail_msg"]).render(
        item=item, o11y_target_tls_cert_dir=cert_dir
    )
    assert f"{cert_dir}/{filename}" in message


@pytest.mark.parametrize("stale", [False, True])
def test_controller_renderer_rejects_unprocessed_tls_markers(stale: bool) -> None:
    tasks = yaml.safe_load(source("tasks/render.yml"))[1]["block"]
    render_index = next(
        index for index, task in enumerate(tasks) if task.get("environment")
    )
    guard = tasks[render_index + 1]["ansible.builtin.assert"]
    environment = jinja2.Environment()  # noqa: S701 - boolean expression only
    compose = "services: {}\n"
    if stale:
        compose = (ANSIBLE.parent / "o11y/compose.yaml").read_text()

    def lookup(_plugin: str, _path: str) -> str:
        return compose

    with patch.dict(environment.globals, lookup=lookup):
        assert (
            environment.compile_expression(guard["that"])(
                o11y_render={"path": "/scratch"}
            )
            is not stale
        )
    assert "controller tbhbops renderer lacks Grafana TLS support" in guard["fail_msg"]
    assert "Install tbhbops from this ops checkout" in guard["fail_msg"]
    assert tasks[render_index + 1]["delegate_to"] == "localhost"
