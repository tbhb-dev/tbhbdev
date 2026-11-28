"""The data types of `tbhbdev alpha agent`: settings, hosts and the run request.

These are plain frozen values. The modules under `decisions/` build and
validate them from values passed in, and no type here reads a file or runs a
process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# The run request's schema version. A reader refuses any other version.
REQUEST_VERSION = 2
DEFAULT_RUN_MEMORY_MIB = 6 * 1024
DEFAULT_MAX_CONCURRENT_RUNS = 3
DEFAULT_MAX_SWAP_IN_PAGES_PER_SECOND = 1000
# The Codex model for a spawned agent that names none: the model that
# tbhb-dev/wiki `models/README.md` pairs with Sonnet (line 58) and names as the
# non-trial Codex builder (line 45). Its `gpt-6.2-sol` is unverified (line 73).
DEFAULT_CODEX_SUBAGENT_MODEL = "gpt-6.1-sol"

# An identifier that reaches a file name, a tmux session name or an argv: run
# names, accounts, profiles and the run's class, variant and role.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# A host name, which may be a dotted ssh name: lower-case labels joined by dots.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
HOST_NAME = re.compile(rf"(?=.{{1,63}}\Z){_HOST_LABEL}(?:\.{_HOST_LABEL})*")


class Invalid(ValueError):  # noqa: N818 -- the message names the invalid value
    """A setting, repo argument or run request that fails validation."""


class RunKind(StrEnum):
    EXEC = "exec"


class Harness(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    AGY = "agy"


class Profile(StrEnum):
    """The purpose of a run."""

    BUILD = "build"
    REVIEW = "review"


class WebSearch(StrEnum):
    LIVE = "live"
    CACHED = "cached"
    DISABLED = "disabled"


class Os(StrEnum):
    LINUX = "linux"
    MACOS = "macos"


@dataclass(frozen=True)
class SandboxPath:
    """The `PATH` entries a run gets on each OS."""

    linux: tuple[str, ...]
    macos: tuple[str, ...]


@dataclass(frozen=True)
class Sandbox:
    audit_dir: str
    path: SandboxPath
    host_path: tuple[str, ...] = ()
    network_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class Codex:
    """Codex's `web_search` setting for each run profile.

    `subagent_model` is the model Codex gives a spawned agent whose spawn names
    none. A spawn that names a model keeps it, so this is a default, not a pin.
    """

    web_search_build: WebSearch
    web_search_review: WebSearch
    subagent_model: str = DEFAULT_CODEX_SUBAGENT_MODEL


@dataclass(frozen=True)
class Credentials:
    """One account's secret references and signing key source."""

    account: str
    gh: str | None
    docker: str | None
    reviews_app_client_id: str | None
    reviews_app_pem: str | None
    signing: str
    pull_requests_app_client_id: str | None = None
    pull_requests_app_pem: str | None = None


@dataclass(frozen=True)
class ClaudeProfile:
    """A Claude auth profile: `name` as config spells it, and its token reference."""

    name: str
    token: str


@dataclass(frozen=True)
class Claude:
    """The profiles, and the name of the one a run uses by default."""

    default_profile: str
    profiles: tuple[ClaudeProfile, ...]


@dataclass(frozen=True)
class Host:
    name: str
    os: Os
    shell: str
    account: str
    accounts: tuple[str, ...]
    tbhbdev: str | None = None
    load_limit: int | None = None
    min_memory_available_percent: int = 20
    max_swap_used_percent: int = 80
    max_memory_pressure_avg10: int = 10
    max_memory_pressure_level: int = 4
    limits_warn_only: bool = False
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    build_shims: bool = True
    auto_select: bool = False
    max_swap_in_pages_per_second: int = DEFAULT_MAX_SWAP_IN_PAGES_PER_SECOND


@dataclass(frozen=True)
class Settings:
    """The validated `[agent]` table. Durations are in seconds unless named."""

    launch_wait_seconds: int
    agy_timeout_seconds: int
    kill_grace_seconds: int
    max_run_minutes: int | None
    sockets_dir: str
    sandbox: Sandbox
    codex: Codex
    credentials: tuple[Credentials, ...]
    claude: Claude
    hosts: tuple[Host, ...]
    agy_build_timeout_seconds: int = 3600
    otel_endpoint: str | None = None
    run_memory_mib: int = DEFAULT_RUN_MEMORY_MIB
    review_memory_mib: int = DEFAULT_RUN_MEMORY_MIB
    worker_path: str | None = None


@dataclass(frozen=True)
class RunPaths:
    """The paths of one run, as absolute strings the caller has resolved.

    `audit_dir` is the run's resolved audit directory, which a review run may
    write. The private `tmpdir` holds tool handoffs. Its sibling `codex_home` holds
    the run's Codex configuration.
    """

    home: str
    worktree: str
    tmpdir: str
    codex_home: str
    audit_dir: str
    signing_socket: str | None = None


@dataclass(frozen=True)
class CodexPolicy:
    """Codex configuration for a full-access run."""

    approval_policy: str
    web_search: WebSearch
    subagent_model: str = DEFAULT_CODEX_SUBAGENT_MODEL


@dataclass(frozen=True)
class ClaudePolicy:
    """Claude's settings, the JSON object that `--settings` takes."""

    settings: dict[str, object]


@dataclass(frozen=True)
class AgyPolicy:
    """agy's project grants, as `read_file(path)` and `write_file(path)` entries."""

    grants: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    """The environment and settings a harness starts with."""

    harness: Harness
    profile: Profile
    env: dict[str, str]
    detail: CodexPolicy | ClaudePolicy | AgyPolicy
    tool_path: str


@dataclass(frozen=True)
class RunRequest:
    """The versioned run request the driver sends and run-host reads.

    It holds every run field and no credential. `commit` is the resolved commit
    of a review run, and `startup_deadline_seconds` bounds the run's startup.
    The schema version is `REQUEST_VERSION`.
    """

    kind: RunKind
    repo: str
    commit: str | None
    harness: Harness
    model: str
    effort: str
    profile: Profile
    claude_profile: str | None
    reason: str | None
    account: str
    timeout: int | None
    class_: str | None
    variant: str | None
    role: str | None
    unsandboxed: bool
    authorized_by: str | None
    startup_deadline_seconds: int
    fetch_refs: tuple[str, ...] = ()
    start_ref: str | None = None
    max_concurrent_runs: int | None = None
    expect_commit: bool = False
    no_commit: bool = False
