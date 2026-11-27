from pathlib import Path
from textwrap import dedent

import pytest

from tbhbops import docker_route as dr

# The nearest ancestor that holds ansible/: mutmut runs these tests from a
# copy under packages/tbhbops/mutants, deeper than the source tree.
ANSIBLE = next(
    path / "ansible"
    for path in Path(__file__).parents
    if (path / "ansible" / "ansible.cfg").is_file()
)

INVENTORY = """
all:
  children:
    nas:
      hosts:
        nas1:
        nas2:
    lab:
      hosts:
        lab1:
"""


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text), encoding="utf-8")


def _tree(
    tmp_path: Path,
    *,
    hosts: str = "nas",
    role_tasks: str = "- name: nothing\n  ansible.builtin.debug:\n    msg: hi\n",
    play_extra: str = "",
) -> Path:
    _write(tmp_path, "inventory/hosts.yml", INVENTORY)
    _write(
        tmp_path,
        "playbooks/p.yml",
        f"""
        - name: Play
          hosts: {hosts}
          roles: [r]
        {play_extra}
        """,
    )
    _write(tmp_path, "roles/r/tasks/main.yml", role_tasks)
    return tmp_path


def test_a_clean_tree_has_no_findings(tmp_path: Path) -> None:
    assert dr.scan(_tree(tmp_path)) == []


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ("privileged: true", "runs privileged"),
        ("privileged: '{{ p }}'", "runs privileged"),
        ("pid_mode: host", "joins the host PID namespace"),
        ("pid: host", "joins the host PID namespace"),
        ("pid_mode: ' host '", "joins the host PID namespace"),
        ("pid_mode: '{{ ns }}'", "joins the host PID namespace"),
        ("network_mode: host", "joins the host network namespace"),
        ("volumes: ['/:/host']", "bind-mounts / or /etc from the host"),
        ("volumes: ['//:/host:ro']", "bind-mounts / or /etc from the host"),
        ("volumes: ['/etc:/e']", "bind-mounts / or /etc from the host"),
        ("volumes: ['/etc/ssh:/e']", "bind-mounts / or /etc from the host"),
        ("volumes: ['/./etc:/e']", "bind-mounts / or /etc from the host"),
        ("volumes: ['/srv/../etc:/e']", "bind-mounts / or /etc from the host"),
        ("volumes: ['{{ d }}:/e']", "bind-mounts / or /etc from the host"),
        (
            "mounts: [{type: bind, source: /etc, target: /e}]",
            "bind-mounts / or /etc from the host",
        ),
        (
            "mounts: [{type: bind, src: /, target: /e}]",
            "bind-mounts / or /etc from the host",
        ),
        ("volumes: ['/var/run/docker.sock:/s']", "mounts a docker.sock"),
        ("env: {SOCK: /run/docker.sock}", "mounts a docker.sock"),
        (
            "project_src: /srv/app",
            "reads a Compose file this check can't scan, so use definition",
        ),
        (
            "definition: {services: {a: {privileged: true}}}",
            "runs privileged",
        ),
        (
            "definition: {services: {a: {volumes: [{type: bind, source: /}]}}}",
            "bind-mounts / or /etc from the host",
        ),
        (
            "definition: {services: {a: {network_mode: host}}}",
            "joins the host network namespace",
        ),
        (
            "definition: {services: [{pid: host}]}",
            "joins the host PID namespace",
        ),
    ],
)
def test_container_module_routes_are_found(
    tmp_path: Path, args: str, problem: str
) -> None:
    tasks = f"""
    - name: Run it
      community.docker.docker_container:
        name: x
        {args}
    """
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == [
        f"roles/r/tasks/main.yml: Run it: {problem}"
    ]


@pytest.mark.parametrize(
    "args",
    [
        "privileged: false",
        "privileged: 'false'",
        "privileged: ' No '",
        "privileged: no",
        "pid_mode: container:other",
        "network_mode: bridge",
        "volumes: ['data:/d', './rel:/r', '/srv/data:/d:ro', '/etcetera:/e']",
        "volumes: ['/srv/example-storage/docker/app:/app']",
        "mounts: [{type: volume, source: /etc, target: /e}]",
        "mounts: [{type: bind, target: /e}]",
        "mounts: ['named:/e']",
        "volumes: {a: b}",
        "volumes: [3]",
        "env: {A: b}",
        "published_ports: [8080]",
    ],
)
def test_container_module_arguments_that_are_safe_pass(
    tmp_path: Path, args: str
) -> None:
    tasks = f"""
    - name: Run it
      docker_container:
        name: x
        {args}
    """
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == []


def test_container_arguments_that_are_not_a_mapping_are_a_finding(
    tmp_path: Path,
) -> None:
    tasks = (
        "- name: Run it\n  community.docker.docker_container: name=x privileged=yes\n"
    )
    finding = (
        "roles/r/tasks/main.yml: Run it: "
        "container arguments aren't a mapping, so it can't be read"
    )
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == [finding]


@pytest.mark.parametrize(
    ("command", "problems"),
    [
        ("docker run --privileged alpine", ["runs privileged"]),
        ("docker run --privileged=true alpine", ["runs privileged"]),
        ("docker run --privileged=truthy alpine", ["runs privileged"]),
        ("docker run --pid=host alpine", ["joins the host PID or network namespace"]),
        ("docker run --pid host alpine", ["joins the host PID or network namespace"]),
        (
            "docker run --network=host alpine",
            ["joins the host PID or network namespace"],
        ),
        ("docker run --net host alpine", ["joins the host PID or network namespace"]),
        ("podman run --net=host alpine", ["joins the host PID or network namespace"]),
        ("docker run -v /:/h alpine", ["bind-mounts / or /etc from the host"]),
        ("docker run -v/:/h alpine", ["bind-mounts / or /etc from the host"]),
        ("docker run -itv /etc:/h alpine", ["bind-mounts / or /etc from the host"]),
        (
            "docker run --volume=/etc/ssh:/h alpine",
            ["bind-mounts / or /etc from the host"],
        ),
        (
            "docker run --volume '/etc:/h' alpine",
            ["bind-mounts / or /etc from the host"],
        ),
        ("docker run -v {{ d }}:/h alpine", ["bind-mounts / or /etc from the host"]),
        (
            "docker run --mount type=bind,source=/,target=/h alpine",
            ["bind-mounts / or /etc from the host"],
        ),
        (
            "docker run --mount=type=bind,src=/etc,dst=/h alpine",
            ["bind-mounts / or /etc from the host"],
        ),
        ("docker run -v /var/run/docker.sock:/s alpine", ["mounts a docker.sock"]),
        (
            "bash -c 'docker run --privileged -v /:/h alpine'",
            ["runs privileged", "bind-mounts / or /etc from the host"],
        ),
    ],
)
@pytest.mark.parametrize(
    "module", ["ansible.builtin.command", "shell", "raw", "ansible.builtin.script"]
)
def test_container_commands_with_a_route_are_found(
    tmp_path: Path, module: str, command: str, problems: list[str]
) -> None:
    tasks = f"- name: Go\n  {module}: {command!r}\n"
    expected = [f"roles/r/tasks/main.yml: Go: {problem}" for problem in problems]
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == sorted(expected)


@pytest.mark.parametrize(
    "command",
    [
        "docker ps",
        "docker run --privileged=false alpine",
        "docker run --privileged=0 alpine",
        "docker run --network=bridge alpine",
        "docker run --pid=container:x alpine",
        "docker run -v /srv/example-storage/docker/app:/app alpine",
        "docker run -v data:/d alpine",
        "docker run -v /etcetera:/d --volume ./x:/y alpine",
        "docker run --mount type=bind,source=/srv,target=/e alpine",
        "echo --privileged -v /:/h --net host",
        "ls -v /etc",
    ],
)
def test_container_commands_that_are_safe_pass(tmp_path: Path, command: str) -> None:
    tasks = f"- name: Go\n  ansible.builtin.command: {command!r}\n"
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == []


@pytest.mark.parametrize(
    "form",
    [
        "cmd: docker run --privileged alpine",
        "argv: [docker, run, --privileged, alpine]",
        "_raw_params: docker run --privileged alpine",
    ],
)
def test_command_mappings_and_lists_are_read(tmp_path: Path, form: str) -> None:
    tasks = f"- name: Go\n  ansible.builtin.command:\n    {form}\n"
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == [
        "roles/r/tasks/main.yml: Go: runs privileged"
    ]


def test_a_command_with_no_text_is_ignored(tmp_path: Path) -> None:
    tasks = (
        "- name: Go\n  ansible.builtin.command:\n"
        "- name: Next\n  ansible.builtin.raw: [1, 2]\n"
    )
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == []


def test_other_modules_that_mention_docker_in_text_pass(tmp_path: Path) -> None:
    tasks = """
    - name: Say docker run --privileged
      ansible.builtin.debug:
        msg: docker run --privileged
      when: docker is defined
    """
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == []


def test_findings_in_blocks_handlers_and_unnamed_tasks_are_found(
    tmp_path: Path,
) -> None:
    tasks = """
    - name: Outer
      block:
        - docker_container: {privileged: true}
      rescue:
        - name: Rescue
          docker_container: {pid_mode: host}
      always:
        - name: Always
          block:
            - name: Deep
              docker_container: {network_mode: host}
    - "not a task"
    """
    _tree(tmp_path, role_tasks=dedent(tasks))
    _write(
        tmp_path,
        "roles/r/handlers/main.yml",
        "- name: Restart\n  docker_container: {privileged: true}\n",
    )
    assert dr.scan(tmp_path) == [
        "roles/r/handlers/main.yml: Restart: runs privileged",
        "roles/r/tasks/main.yml: (unnamed): runs privileged",
        "roles/r/tasks/main.yml: Deep: joins the host network namespace",
        "roles/r/tasks/main.yml: Rescue: joins the host PID namespace",
    ]


def test_play_level_tasks_are_scanned(tmp_path: Path) -> None:
    extra = """
          pre_tasks:
            - name: Pre
              docker_container: {privileged: true}
          tasks:
            - name: Main
              docker_container: {privileged: true}
          post_tasks:
            - name: Post
              docker_container: {privileged: true}
          handlers:
            - name: Handle
              docker_container: {privileged: true}
    """
    _tree(tmp_path)
    _write(
        tmp_path,
        "playbooks/p.yml",
        "- name: Play\n  hosts: nas\n" + dedent(extra).replace("\n", "\n  "),
    )
    assert dr.scan(tmp_path) == [
        f"playbooks/p.yml: {name}: runs privileged"
        for name in ("Handle", "Main", "Post", "Pre")
    ]


@pytest.mark.parametrize(
    ("hosts", "targets"),
    [
        ("nas", True),
        ("all", True),
        ("'*'", True),
        ("nas1", True),
        ("nas2:lab", True),
        ("lab,nas1", True),
        ("'lab : nas'", True),
        ("lab", False),
        ("lab1", False),
        ("'all:!nas'", False),
        ("'nas:!nas1'", True),
        ("'lab:&nas'", False),
        ("'!nas'", False),
        ("nasty", False),
    ],
)
def test_only_plays_that_target_a_nas_are_scanned(
    tmp_path: Path, hosts: str, targets: bool
) -> None:
    tasks = "- name: Bad\n  docker_container: {privileged: true}\n"
    found = dr.scan(_tree(tmp_path, hosts=hosts, role_tasks=tasks))
    assert found == (
        ["roles/r/tasks/main.yml: Bad: runs privileged"] if targets else []
    )


def test_only_the_nas_group_in_the_inventory_names_hosts(tmp_path: Path) -> None:
    _tree(tmp_path, hosts="lab1")
    assert dr.scan(tmp_path) == []


def test_roles_come_in_every_spelling_and_through_dependencies_and_includes(
    tmp_path: Path,
) -> None:
    bad = "- name: Bad\n  docker_container: {privileged: true}\n"
    _tree(tmp_path)
    _write(
        tmp_path,
        "playbooks/p.yml",
        """
        - name: Play
          hosts: nas
          roles:
            - dep
            - role: via_include
            - name: dict_name
        - name: Not a NAS
          hosts: lab
          roles: [skipped]
        - "not a play"
        """,
    )
    _write(
        tmp_path,
        "roles/dep/meta/main.yml",
        "dependencies: [leaf, {role: leaf2}, {name: leaf3}]\n",
    )
    _write(tmp_path, "roles/leaf/tasks/main.yml", bad)
    _write(tmp_path, "roles/leaf2/tasks/extra.yml", bad)
    _write(tmp_path, "roles/leaf3/meta/main.yml", "dependencies:\n")
    _write(tmp_path, "roles/leaf3/tasks/main.yml", bad)
    _write(
        tmp_path,
        "roles/via_include/tasks/main.yml",
        """
        - ansible.builtin.include_role:
            name: included
        - import_role:
            name: imported
        - ansible.builtin.include_role: included
        - ansible.builtin.import_tasks: other.yml
        """,
    )
    _write(tmp_path, "roles/included/tasks/main.yml", bad)
    _write(tmp_path, "roles/imported/tasks/main.yml", bad)
    _write(tmp_path, "roles/dict_name/tasks/main.yml", "")
    _write(tmp_path, "roles/skipped/tasks/main.yml", bad)
    assert dr.scan(tmp_path) == [
        "roles/imported/tasks/main.yml: Bad: runs privileged",
        "roles/included/tasks/main.yml: Bad: runs privileged",
        "roles/leaf/tasks/main.yml: Bad: runs privileged",
        "roles/leaf2/tasks/extra.yml: Bad: runs privileged",
        "roles/leaf3/tasks/main.yml: Bad: runs privileged",
    ]


def test_a_role_is_scanned_once(tmp_path: Path) -> None:
    bad = "- name: Bad\n  docker_container: {privileged: true}\n"
    _tree(tmp_path, role_tasks=bad)
    _write(
        tmp_path,
        "playbooks/p.yml",
        "- name: A\n  hosts: nas\n  roles: [r, r]\n"
        "- name: B\n  hosts: all\n  roles: [r]\n",
    )
    assert dr.scan(tmp_path) == ["roles/r/tasks/main.yml: Bad: runs privileged"]


def test_a_play_with_no_roles_and_a_missing_role_directory_pass(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(
        tmp_path,
        "playbooks/p.yml",
        "- name: A\n  hosts: nas\n- name: B\n  hosts: nas\n  roles: [absent]\n",
    )
    assert dr.scan(tmp_path) == []


def test_an_inventory_without_a_nas_group_is_an_error(tmp_path: Path) -> None:
    _tree(tmp_path)
    _write(
        tmp_path,
        "inventory/hosts.yml",
        "all:\n  children:\n    lab:\n      hosts: {}\n",
    )
    with pytest.raises(KeyError, match="nas"):
        dr.scan(tmp_path)


def test_podman_modules_are_read_like_docker_modules(tmp_path: Path) -> None:
    tasks = "- name: Run it\n  containers.podman.podman_container: {privileged: true}\n"
    assert dr.scan(_tree(tmp_path, role_tasks=tasks)) == [
        "roles/r/tasks/main.yml: Run it: runs privileged"
    ]
