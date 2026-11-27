"""Check real Ansible variable precedence locally, without SSH connections."""

import configparser
import shlex
from pathlib import Path

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "ansible.cfg").is_file()
)


def test_ssh_defaults_disable_the_agent_and_preserve_connection_reuse() -> None:
    config = configparser.ConfigParser(interpolation=None)
    config.read(ANSIBLE / "ansible.cfg")
    assert shlex.split(config["ssh_connection"]["ssh_args"]) == [
        "-C",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60s",
        "-o",
        "IdentityAgent=none",
    ]
