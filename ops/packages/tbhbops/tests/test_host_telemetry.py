"""Check how the host telemetry template labels journal entries."""

from pathlib import Path

from jinja2 import Template

# Mutmut runs this test from a deeper copy under packages/tbhbops/mutants.
TEMPLATE_PATH = Path("ansible/roles/host_telemetry/templates/config.alloy.j2")
TEMPLATE = next(
    path / TEMPLATE_PATH
    for path in Path(__file__).parents
    if (path / TEMPLATE_PATH).is_file()
)


def test_event_gateway_tags_take_precedence_over_docker_unit() -> None:
    config = Template(TEMPLATE.read_text(encoding="utf-8")).render(
        ansible_managed="Ansible managed",
        host_telemetry_otlp_endpoint="http://otlp.example.example.invalid:4318",
    )
    tag_rule = (
        '    source_labels = ["__journal_syslog_identifier"]\n'
        '    regex         = "(event-gateway-gateway|event-gateway-tunnel)"\n'
        '    target_label  = "service_name"\n'
        '    replacement   = "$1"\n'
    )
    assert (
        config.index(r'"(docker|libvirtd|tailscaled)\\.service"')
        < config.index(tag_rule)
        < config.index('action        = "keep"')
    )
