"""Find the ways an Ansible play on a NAS could reach root through Docker.

Membership in `docker` already gives root on nas-a and nas-b, so
Ansible must never use it as a route there. Managing the NAS's own containers
is fine. This module scans every play that targets the `nas` group, and the
roles those plays use, for a container task that:

- runs privileged,
- joins the host's PID or network namespace,
- bind-mounts `/`, `/etc` or anything under `/etc` from the host, or
- mounts a docker.sock.

A task it can't read, such as a templated value or a Compose project in a
file, counts as a finding, because the check can't show it is safe. It reads
container modules' arguments and the text of command, shell, raw and script
tasks that mention docker or podman.
"""

import re
from typing import cast
lazy from collections.abc import Iterator
lazy from pathlib import Path

import yaml

NAS = "nas"
_EVERYONE = frozenset({"all", "*", NAS})
_BLOCKS = ("block", "rescue", "always")
_TASK_LISTS = ("pre_tasks", "tasks", "post_tasks", "handlers")
_COMMANDS = frozenset({"command", "shell", "raw", "script"})
_ROLE_TASKS = frozenset({"import_role", "include_role"})

_CONTAINER_CLI = re.compile(r"\b(?:docker|podman)\b")
_PRIVILEGED = re.compile(r"--privileged(?!=(?:false|0)\b)")
_HOST_NAMESPACE = re.compile(r"--(?:pid|network|net)(?:=|\s+)host\b")
# The source of `-v SRC:DST` (also `-itv`, `--volume=`) and of
# `--mount ...source=SRC`. A template stands in for any source.
_BIND = re.compile(
    r"""(?<!\S)(?:-[A-Za-z]*v|--volume)[=\s]*["']?(?P<src>\{\{|[^:\s"']+(?=:))"""
    r"""|--mount[=\s]+[^\n]*?\b(?:source|src)=(?P<mount>[^,\s"']+)"""
)

type Mapping = dict[str, object]

_INVENTORY = ("inventory", "hosts.yml")
_META = ("meta", "main.yml")
_PLAYBOOKS = "playbooks"
_ROLES = "roles"
_ROLE_DIRS = ("tasks", "handlers")


def _as_list(value: object) -> list[object]:
    return cast("list[object]", value)  # pragma: no mutate -- a type-checker string


def _as_mapping(value: object) -> Mapping:
    return cast("Mapping", value)  # pragma: no mutate -- a type-checker string


def _short(module: str) -> str:
    """Return a module's name without its collection, as in `include_role`."""
    return module.rpartition(".")[2]


def _load(path: Path) -> object:
    # `encoding` mutants only matter on a host whose default isn't UTF-8.
    text = path.read_text(encoding="utf-8")  # pragma: no mutate
    data: object = yaml.safe_load(text)
    return data


def _mappings(items: object) -> Iterator[Mapping]:
    """Yield the mappings in a list, such as the plays of a playbook."""
    if isinstance(items, list):
        for item in _as_list(items):
            if isinstance(item, dict):
                yield _as_mapping(item)


def _tasks(items: object) -> Iterator[Mapping]:
    """Yield every task in a task list, descending into blocks."""
    for task in _mappings(items):
        yield task
        for key in _BLOCKS:
            yield from _tasks(task.get(key))


def _off(value: object) -> bool:
    return value is False or (
        isinstance(value, str) and value.strip().lower() in {"false", "no"}
    )


def _host(value: object) -> bool:
    return isinstance(value, str) and (value.strip() == "host" or "{{" in value)


def _bad_source(source: object) -> bool:
    """Say whether a host path is `/`, `/etc`, below `/etc`, or unknown."""
    text = str(source)
    parts = [part for part in text.split("/") if part not in {"", "."}]
    return "{{" in text or (
        text.startswith("/") and (not parts or parts[0] == "etc" or ".." in parts)
    )


def _sources(value: object) -> Iterator[object]:
    """Yield the host side of each volume or bind mount in a list."""
    if isinstance(value, list):
        for entry in _as_list(value):
            if isinstance(entry, str):
                yield entry.split(":", 1)[
                    0
                ]  # pragma: no mutate -- the first field either way
            elif isinstance(entry, dict):
                mount = _as_mapping(entry)
                if mount.get("type") == "bind":
                    yield mount.get("source") or mount.get("src")


def _keys(node: Mapping) -> Iterator[str]:
    for key, value in node.items():
        if key == "privileged" and not _off(value):
            yield "runs privileged"
        elif key in {"pid_mode", "pid"} and _host(value):
            yield "joins the host PID namespace"
        elif key == "network_mode" and _host(value):
            yield "joins the host network namespace"
        elif key in {"volumes", "mounts"} and any(map(_bad_source, _sources(value))):
            yield "bind-mounts / or /etc from the host"
        elif key == "project_src":
            yield "reads a Compose file this check can't scan, so use definition"


def _walk(node: object) -> Iterator[str]:
    if isinstance(node, str):
        if "docker.sock" in node:
            yield "mounts a docker.sock"
    elif isinstance(node, list):
        for item in _as_list(node):
            yield from _walk(item)
    elif isinstance(node, dict):
        mapping = _as_mapping(node)
        yield from _keys(mapping)
        for value in mapping.values():
            yield from _walk(value)


def _text(args: object) -> str:
    if isinstance(args, str):
        return args
    if isinstance(args, list):
        return " ".join(map(str, _as_list(args)))
    if isinstance(args, dict):
        mapping = _as_mapping(args)
        return " ".join(
            _text(mapping.get(key)) for key in ("cmd", "_raw_params", "argv")
        )
    return ""  # pragma: no mutate -- text without docker is skipped


def _command(text: str) -> Iterator[str]:
    if not _CONTAINER_CLI.search(text):
        return
    if _PRIVILEGED.search(text):
        yield "runs privileged"
    if _HOST_NAMESPACE.search(text):
        yield "joins the host PID or network namespace"
    if "docker.sock" in text:
        yield "mounts a docker.sock"
    for match in _BIND.finditer(text):
        if _bad_source(match["src"] or match["mount"]):
            yield "bind-mounts / or /etc from the host"


def _problems(task: Mapping) -> Iterator[str]:
    for module, args in task.items():
        name = _short(module)
        if "docker" in name or "podman" in name:
            if isinstance(args, dict):
                yield from _walk(args)
            else:
                yield "container arguments aren't a mapping, so it can't be read"
        elif name in _COMMANDS:
            yield from _command(_text(args))


def _role_name(entry: object) -> str:
    if isinstance(entry, dict):
        mapping = _as_mapping(entry)
        return str(mapping.get("role") or mapping.get("name"))
    return str(entry)


def _used_roles(tasks: list[Mapping]) -> Iterator[str]:
    for task in tasks:
        for module, args in task.items():
            if _short(module) in _ROLE_TASKS and isinstance(args, dict):
                yield str(_as_mapping(args).get("name"))


def _targets_nas(hosts: object, names: frozenset[str]) -> bool:
    """Say whether a play's `hosts` pattern selects a NAS."""
    terms = {term.strip() for term in re.split(r"[:,]", str(hosts)) if term.strip()}
    excluded = {term[1:] for term in terms if term[0] == "!"}
    return bool(terms & names) and NAS not in excluded


def _nas_names(ansible: Path) -> frozenset[str]:
    """Return the hosts of the inventory's nas group, with the words that mean all."""
    inventory = _as_mapping(_load(ansible.joinpath(*_INVENTORY)))
    everything = _as_mapping(inventory["all"])
    children = _as_mapping(everything["children"])
    group = _as_mapping(children[NAS])
    return _EVERYONE | frozenset(_as_mapping(group["hosts"]))


def _role_files(directory: Path) -> Iterator[tuple[Path, object]]:
    for folder in _ROLE_DIRS:
        for path in sorted((directory / folder).glob("*.yml")):
            yield path, _load(path)


def _dependencies(directory: Path) -> Iterator[str]:
    meta = directory.joinpath(*_META)
    if meta.is_file():
        dependencies = _as_mapping(_load(meta)).get("dependencies")
        yield from map(_role_name, _as_list(dependencies or []))


def _nas_work(
    ansible: Path, names: frozenset[str]
) -> tuple[list[str], list[tuple[Path, object]]]:
    """Return the roles and task lists of every play that selects a NAS."""
    roles: list[str] = []
    task_lists: list[tuple[Path, object]] = []
    for playbook in sorted((ansible / _PLAYBOOKS).glob("*.yml")):
        for play in _mappings(_load(playbook)):
            if _targets_nas(play["hosts"], names):
                task_lists.extend((playbook, play.get(key)) for key in _TASK_LISTS)
                roles.extend(map(_role_name, _as_list(play.get("roles", []))))
    return roles, task_lists


def _open_role(ansible: Path, role: str) -> tuple[list[str], list[tuple[Path, object]]]:
    """Return a role's dependencies and its task and handler files."""
    directory = ansible / _ROLES / role
    return list(_dependencies(directory)), list(_role_files(directory))


def scan(ansible: Path) -> list[str]:
    """Return a finding for each container route to root in the NAS plays.

    `ansible` is the repository's ansible/ directory. Each finding names the
    file, the task and the problem, and the list is sorted.
    """
    roles, task_lists = _nas_work(ansible, _nas_names(ansible))
    seen: set[str] = set()
    findings: list[str] = []
    while roles or task_lists:
        if roles:
            role = roles.pop()
            if role not in seen:
                seen.add(role)
                dependencies, files = _open_role(ansible, role)
                roles.extend(dependencies)
                task_lists.extend(files)
            continue
        path, items = task_lists.pop()
        tasks = list(_tasks(items))
        roles.extend(_used_roles(tasks))
        findings.extend(
            f"{path.relative_to(ansible)}: {task.get('name', '(unnamed)')}: {problem}"
            for task in tasks
            for problem in _problems(task)
        )
    return sorted(findings)
