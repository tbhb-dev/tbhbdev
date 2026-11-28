"""Fail when a task names a template or file its role does not carry."""

from pathlib import Path

import pytest
import yaml

ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "roles").is_dir()
)
ROLES = ANSIBLE / "roles"
MODULES = {"ansible.builtin.copy", "ansible.builtin.template"}


def _tasks(node):
    """Yield every task mapping, including those nested in blocks."""
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always"):
            yield from _tasks(node.get(key, []))


def _missing(role: Path) -> list[str]:
    missing = []
    for tasks_file in sorted((role / "tasks").glob("*.yml")):
        for task in _tasks(yaml.safe_load(tasks_file.read_text()) or []):
            for module in MODULES & task.keys():
                src = (task[module] or {}).get("src")
                if not isinstance(src, str) or "{{" in src:
                    continue
                folder = "templates" if module.endswith("template") else "files"
                if not (role / folder / src).is_file():
                    missing.append(f"{tasks_file.name}: {folder}/{src}")
    return missing


@pytest.mark.parametrize(
    "role", sorted(p for p in ROLES.iterdir() if p.is_dir()), ids=lambda p: p.name
)
def test_task_sources_exist(role: Path) -> None:
    assert _missing(role) == []
