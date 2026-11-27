"""Build the run environment and the harness tool search path."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.decisions.policy_paths import resolve
from tbhbdev.alpha.agent.model import Harness, Os, Profile

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, RunPaths, Settings

LINUX_LANG = "C.UTF-8"
MACOS_LANG = "en_US.UTF-8"
# The handoff directories, under the run's private TMPDIR. run-host creates
# each one, and the child gets its path in the variable of the same tool.
GH_CONFIG_SUBDIR = "gh"
DOCKER_CONFIG_SUBDIR = "docker"
# pytest-xdist reads this for `-n auto`, so a run's test fan-out stays bounded.
XDIST_WORKERS = "4"


def environment(
    settings: Settings, host: Host, harness: Harness, paths: RunPaths, account: str
) -> dict[str, str]:
    """The child's whole environment: an allowlist, with no parent values.

    The two config-directory variables name directories under the private
    TMPDIR that run-host creates and fills. They hold no credential.
    """
    env = {
        "PATH": ":".join(host_tools.search_path(settings, host.os)),
        "HOME": paths.home,
        "USER": account,
        "LOGNAME": account,
        "SHELL": f"/bin/{host.shell}",
        "LANG": LINUX_LANG if host.os is Os.LINUX else MACOS_LANG,
        "TMPDIR": paths.tmpdir,
        "CLAUDE_CODE_TMPDIR": paths.tmpdir,
        "TMPPREFIX": resolve(paths.tmpdir, "zsh"),
        "DOCKER_CONFIG": resolve(paths.tmpdir, DOCKER_CONFIG_SUBDIR),
        "GH_CONFIG_DIR": resolve(paths.tmpdir, GH_CONFIG_SUBDIR),
        "PYTEST_DEBUG_TEMPROOT": paths.tmpdir,
        "PYTEST_XDIST_AUTO_NUM_WORKERS": XDIST_WORKERS,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "7",
    }
    overrides = (
        ("core.fsmonitor", "false"),
        ("safe.bareRepository", "explicit"),
        ("diff.ignoreSubmodules", "all"),
        ("status.submoduleSummary", "false"),
        ("protocol.ext.allow", "never"),
        # `user` lets the run's own `git fetch` read its mirror but refuses the
        # file transport for a submodule or any other git-started fetch.
        ("protocol.file.allow", "user"),
        ("diff.external", ""),
    )
    for index, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    if harness is Harness.CODEX:
        env["CODEX_HOME"] = paths.codex_home
    if paths.signing_socket is not None:
        env["SSH_AUTH_SOCK"] = paths.signing_socket
    return env


def review_environment(profile: Profile, paths: RunPaths) -> dict[str, str]:
    if profile is Profile.REVIEW:
        return {"GH_CONFIG_DIR": resolve(paths.tmpdir, GH_CONFIG_SUBDIR)}
    return {}


def tool_path(settings: Settings, host: Host, home: str) -> str:
    search = settings.sandbox.path
    entries = search.linux if host.os is Os.LINUX else search.macos
    return ":".join(resolve(home, e) for e in entries)
