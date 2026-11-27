"""Keep the deploy runner job guard to main commits of the allowed repository."""

import ast
import base64
import contextlib
import importlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import threading
import time
import types
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import pytest

if TYPE_CHECKING:
    from collections.abc import Buffer, Generator
    from types import ModuleType

# POSIX-only calls, which the tests that make them skip without.
POSIX_OS = importlib.import_module("os")

ROLE = next(
    path / "ansible" / "roles" / "deploy_runner"
    for path in Path(__file__).resolve().parents
    if (path / "ansible" / "roles").is_dir()
)
GUARD_PATH = ROLE / "files" / "deploy-runner-job-guard"
SLOT_PATH = ROLE.parent / "actions_runner_pool" / "files" / "actions-runner-slot.sh"
SHA = "a" * 40
KEYS = {"deploy-key": b"DEPLOY PRIVATE KEY\n", "repo-key": b"REPO PRIVATE KEY\n"}
LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux",
    reason="memfd_create, /proc and PR_SET_PDEATHSIG exist only on Linux",
)


def _load() -> ModuleType:
    loader = SourceFileLoader("deploy_runner_job_guard", str(GUARD_PATH))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


guard = _load()


def _settings(tmp_path: Path) -> dict[str, object]:
    return {
        "repository": "tbhb-dev/tbhbdev",
        "ref": "refs/heads/main",
        "events": ["push", "workflow_dispatch"],
        "url": "git@example.invalid:tbhb-dev/tbhbdev.git",
        "known_hosts": "/nonexistent/known_hosts",
        "cache": str(tmp_path / "cache" / "repo.git"),
    }


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_REPOSITORY": "tbhb-dev/tbhbdev",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": SHA,
    }
    return env | overrides


def _tar(members: dict[str, bytes]) -> bytes:
    """A tar stream like the slot script's: tar -c . of the secrets directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        archive.addfile(tarfile.TarInfo("./"), None)
        archive.getmember("./").type = tarfile.DIRTYPE
        for name, data in members.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _stream(members: dict[str, bytes]) -> io.BytesIO:
    """What the shim passes on standard input: base64 of the tar, joined."""
    return io.BytesIO(base64.b64encode(_tar(members)))


class _Git:
    """Stand in for _run: records calls and answers by subcommand."""

    def __init__(self, results: dict[str, int]) -> None:
        self.results = results
        self.calls: list[list[str]] = []
        self.key_path: str | None = None
        self.fetch_key: bytes | None = None

    def __call__(
        self, argv: list[str], env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        name = next(word for word in ("init", "fetch", "merge-base") if word in argv)
        if name == "fetch":
            match = re.search(r"-i (\S+)", env["GIT_SSH_COMMAND"])
            assert match
            self.key_path = match.group(1)
            # A memory file, reached only through the guard's own descriptor.
            assert re.fullmatch(r"/proc/\d+/fd/\d+", self.key_path)
            if sys.platform == "linux":
                self.fetch_key = Path(self.key_path).read_bytes()
        code = self.results.get(name, 0)
        return subprocess.CompletedProcess(argv, code, "", "boom" if code else "")


class _Job:
    """The job's mounts: settings, the shim's stream and the empty keys directory."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = tmp_path / "guard.json"
        self.settings.write_text(json.dumps(_settings(tmp_path)), encoding="utf-8")
        self.stream = _stream(KEYS)
        self.keys_dir = tmp_path / "keys"
        self.keys_dir.mkdir(mode=0o700)
        self.cache = Path(str(_settings(tmp_path)["cache"]))
        # The test process stands in for the shim: its parent is the "shim".
        self.argv = ["job-guard", str(os.getppid())]

    def main(self, env: dict[str, str]) -> int:
        return int(
            guard.main(self.argv, env, self.settings, self.stream, self.keys_dir)
        )

    def written(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.keys_dir.iterdir()}


@contextlib.contextmanager
def _fake_memory_key(key: bytes) -> Generator[Path]:
    """Off Linux, where memfd_create is missing, a path of the same form."""
    del key
    yield Path(f"/proc/{os.getpid()}/fd/3")


@pytest.fixture(autouse=True)
def _stand_in_for_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the tests of main from tying the test process to its parent.

    The tests of the two functions themselves, and the kill test, use the real
    ones on Linux.
    """
    monkeypatch.setattr(guard, "_die_with_parent", lambda: None)
    if sys.platform != "linux":
        monkeypatch.setattr(guard, "_memory_key", _fake_memory_key)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    results: dict[str, int] | None = None,
    *,
    cache_exists: bool = True,
) -> tuple[int, _Git, _Job]:
    job = _Job(tmp_path)
    if cache_exists:
        job.cache.mkdir(parents=True)
    git = _Git(results or {})
    monkeypatch.setattr(guard, "_run", git)
    return job.main(env), git, job


def test_allows_a_main_push_whose_commit_is_on_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, git, job = _run(tmp_path, monkeypatch, _env())
    assert code == 0
    assert capsys.readouterr().err == ""
    assert [call[3] for call in git.calls] == ["fetch", "merge-base"]
    assert git.calls[1][-3:] == ["--is-ancestor", SHA, "refs/remotes/origin/main"]
    # Only now do the keys exist, each readable by its owner alone.
    assert job.written() == KEYS
    for path in job.keys_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The fetch used the repo key, from a memory file that is gone again.
    if sys.platform == "linux":
        assert git.fetch_key == KEYS["repo-key"]
        assert git.key_path is not None
        assert not Path(git.key_path).exists()
    # Nothing but the two keys went anywhere on disk: no scratch directory.
    assert {path for path in tmp_path.rglob("*") if path.is_file()} == {
        job.settings,
        *job.keys_dir.iterdir(),
    }


def test_allows_a_manual_dispatch_and_creates_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, git, job = _run(
        tmp_path,
        monkeypatch,
        _env(GITHUB_EVENT_NAME="workflow_dispatch"),
        cache_exists=False,
    )
    assert code == 0
    assert git.calls[0][:3] == ["git", "init", "--bare"]
    assert job.written() == KEYS


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        (_env(GITHUB_REPOSITORY="tbhb-dev/other"), "repository"),
        (_env(GITHUB_REF="refs/heads/topic"), "ref"),
        (_env(GITHUB_REF="refs/pull/1/merge"), "ref"),
        (_env(GITHUB_EVENT_NAME="pull_request"), "event"),
        (_env(GITHUB_SHA="main"), "full commit id"),
        (_env(GITHUB_SHA="A" * 40), "full commit id"),
        ({}, "repository"),
    ],
)
def test_refuses_the_wrong_context_before_any_git_call_and_writes_no_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    reason: str,
) -> None:
    code, git, job = _run(tmp_path, monkeypatch, env)
    assert code == 1
    assert reason in capsys.readouterr().err
    assert git.calls == []
    assert job.written() == {}


def test_refuses_a_commit_not_on_main_and_writes_no_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, job = _run(tmp_path, monkeypatch, _env(), {"merge-base": 1})
    assert code == 1
    assert f"commit {SHA} is not on origin/main" in capsys.readouterr().err
    assert job.written() == {}


def test_refuses_when_the_fetch_fails_and_writes_no_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, git, job = _run(tmp_path, monkeypatch, _env(), {"fetch": 128})
    assert code == 1
    assert "cannot verify against origin/main: boom" in capsys.readouterr().err
    assert len(git.calls) == 1
    assert job.written() == {}


def test_refuses_when_the_cache_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, job = _run(tmp_path, monkeypatch, _env(), {"init": 1}, cache_exists=False)
    assert code == 1
    assert "cannot create the cache: boom" in capsys.readouterr().err
    assert job.written() == {}


def test_refuses_when_git_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _Job(tmp_path)
    job.cache.mkdir(parents=True)

    def _timeout(argv: list[str], env: dict[str, str]) -> None:
        del env
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(guard, "_run", _timeout)
    assert job.main(_env()) == 1
    assert "timed out" in capsys.readouterr().err
    assert job.written() == {}


def test_refuses_without_readable_settings(tmp_path: Path) -> None:
    job = _Job(tmp_path)
    job.settings = tmp_path / "missing.json"
    assert job.main(_env()) == 1
    job.settings = tmp_path / "broken.json"
    job.settings.write_text("{", encoding="utf-8")
    job.stream = _stream(KEYS)
    assert job.main(_env()) == 1
    job.settings = tmp_path / "partial.json"
    job.settings.write_text("{}", encoding="utf-8")
    job.stream = _stream(KEYS)
    assert job.main(_env()) == 1
    assert job.written() == {}


def test_main_reads_the_process_environment_and_arguments_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _env(GITHUB_REF="refs/heads/topic").items():
        monkeypatch.setenv(name, value)
    job = _Job(tmp_path)
    monkeypatch.setattr(guard.sys, "argv", job.argv)
    monkeypatch.setattr(guard.sys, "stdin", types.SimpleNamespace(buffer=job.stream))
    assert guard.main(None, None, job.settings, None, job.keys_dir) == 1
    # The default stream was read, so the refusal is the ref's.
    assert job.stream.read() == b""
    assert job.written() == {}


def test_the_paths_are_the_ones_the_slot_script_mounts() -> None:
    assert Path("/run/job-secrets") == guard.KEYS_DIR
    assert Path("/etc/actions-runner/job/guard.json") == guard.SETTINGS
    assert guard.KEY_NAMES == ("deploy-key", "repo-key")
    assert not hasattr(guard, "SECRETS")


def test_git_runs_with_a_pinned_ssh_command_and_no_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def _record(
        argv: list[str], env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = env
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(guard, "_run", _record)
    guard._git(_settings(tmp_path), Path("/some/key"), "fetch")  # noqa: SLF001
    env = cast("dict[str, str]", captured["env"])
    command = env["GIT_SSH_COMMAND"]
    assert command.startswith("ssh -F /dev/null -i /some/key ")
    for option in (
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "BatchMode=yes",
        "ControlMaster=no",
        "ControlPath=none",
        "ConnectTimeout=20",
        "ServerAliveInterval=10",
        "ServerAliveCountMax=3",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=/nonexistent/known_hosts",
    ):
        assert f"-o {option}" in command
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["PATH"] == "/usr/bin:/bin"
    assert captured["argv"] == [
        "git",
        "--git-dir",
        str(tmp_path / "cache" / "repo.git"),
        "fetch",
    ]


def test_the_settings_no_longer_name_a_key() -> None:
    source = GUARD_PATH.read_text(encoding="utf-8")
    assert "settings['key']" not in source
    assert 'settings["key"]' not in source


# Python 3.12, the runner image's, evaluates annotations where it defines them.


def _type_checking_imports(tree: ast.AST) -> set[str]:
    hidden: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and (
            ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
        ):
            imports = [
                n for n in ast.walk(node) if isinstance(n, ast.Import | ast.ImportFrom)
            ]
            hidden.update(
                (alias.asname or alias.name).split(".")[0]
                for imported in imports
                for alias in imported.names
            )
    return hidden


def _function_annotations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.expr]:
    arguments = node.args
    every = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        *[arg for arg in (arguments.vararg, arguments.kwarg) if arg],
    ]
    found = [arg.annotation for arg in every if arg.annotation]
    if node.returns:
        found.append(node.returns)
    return found


def _type_checking_names_in_annotations(source: str) -> set[str]:
    """Names imported under TYPE_CHECKING that an unquoted annotation uses."""
    tree = ast.parse(source)
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotations.extend(_function_annotations(node))
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    used = {
        name.id
        for annotation in annotations
        # A string is not evaluated, so only the bare expressions count.
        if not isinstance(annotation, ast.Constant)
        for name in ast.walk(annotation)
        if isinstance(name, ast.Name)
    }
    return _type_checking_imports(tree) & used


def test_no_type_checking_import_is_used_by_an_evaluated_annotation() -> None:
    source = GUARD_PATH.read_text(encoding="utf-8")
    assert _type_checking_names_in_annotations(source) == set()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The shape of the bug: Python 3.12 raises NameError at the def.
        (
            (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from collections.abc import Mapping\n"
                "def f(x: Mapping[str, int]) -> None: ...\n"
            ),
            {"Mapping"},
        ),
        (
            (
                "import typing\n"
                "if typing.TYPE_CHECKING:\n"
                "    import collections.abc as abc\n"
                "def f() -> abc.Mapping: ...\n"
            ),
            {"abc"},
        ),
        (
            (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from typing import IO\n"
                "x: list[IO[bytes]] = []\n"
            ),
            {"IO"},
        ),
        (
            (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from collections.abc import Mapping\n"
                "def f(*a: Mapping, k: Mapping = None, **kw: Mapping) -> None: ...\n"
            ),
            {"Mapping"},
        ),
        # Quoted annotations, and casts, are not evaluated.
        (
            (
                "from typing import TYPE_CHECKING, cast\n"
                "if TYPE_CHECKING:\n"
                "    from collections.abc import Mapping\n"
                "def f(x: 'Mapping[str, int]') -> 'Mapping':\n"
                "    return cast('Mapping', x)\n"
            ),
            set(),
        ),
        # A runtime import is fine.
        (
            (
                "from collections.abc import Mapping\n"
                "def f(x: Mapping[str, int]) -> None: ...\n"
            ),
            set(),
        ),
    ],
)
def test_the_annotation_check_catches_the_failure_it_exists_for(
    source: str, expected: set[str]
) -> None:
    assert _type_checking_names_in_annotations(source) == expected


def test_the_guard_parses_as_python_3_12() -> None:
    source = GUARD_PATH.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 12))
    assert "from __future__" not in source


def test_the_guard_writes_the_fetch_key_to_no_file() -> None:
    source = GUARD_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imported & {"tempfile", "shutil"}
    assert "mkdtemp" not in source
    assert "memfd_create" in source


# The keys' delivery.


def test_drain_ignores_everything_but_the_two_key_files() -> None:
    link = tarfile.TarInfo("./deploy-key.pub")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        archive.addfile(link)
    raw = _tar({**KEYS, "deploy-key.pub": b"public\n", "extra": b"x"})
    stream = io.BytesIO(base64.b64encode(raw + buffer.getvalue()))
    assert guard.drain_secrets(stream) == KEYS


@pytest.mark.parametrize(
    "members",
    [
        {},
        {"deploy-key": b"x"},
        {"repo-key": b"x"},
        {"deploy-key": b"x", "repo-key": b""},
    ],
)
def test_drain_refuses_a_missing_or_empty_key(members: dict[str, bytes]) -> None:
    with pytest.raises(guard.RefusalError, match="missing or empty keys"):
        guard.drain_secrets(_stream(members))


def test_drain_refuses_a_stream_that_is_not_tar() -> None:
    stream = io.BytesIO(base64.b64encode(b"this is not an archive" * 100))
    with pytest.raises(guard.RefusalError, match="didn't parse"):
        guard.drain_secrets(stream)


@pytest.mark.parametrize(
    "data",
    [
        b"not base64!",
        base64.b64encode(b"abc") + b"\n",
        base64.b64encode(b"abc")[:3],
        b" " + base64.b64encode(b"abc"),
        b"\xff\xfe",
    ],
)
def test_drain_refuses_what_is_not_base64(data: bytes) -> None:
    with pytest.raises(guard.RefusalError, match="didn't decode"):
        guard.drain_secrets(io.BytesIO(data))


def test_drain_refuses_a_stream_over_the_size_bound_without_reading_it_all() -> None:
    class _Endless(io.RawIOBase):
        reads = 0

        @override
        def readable(self) -> bool:
            return True

        @override
        def readinto(self, buffer: Buffer, /) -> int:
            type(self).reads += 1
            view = memoryview(buffer)
            view[:] = b"A" * len(view)
            return len(view)

    stream = io.BufferedReader(_Endless(), buffer_size=4096)
    with pytest.raises(guard.RefusalError, match="longer than 65536 bytes"):
        guard.drain_secrets(stream)
    assert _Endless.reads * 4096 < 2 * guard.MAX_STREAM


def test_drain_accepts_a_stream_of_exactly_the_bound() -> None:
    # The bound is a refusal of longer streams: this one gets as far as its content.
    stream = io.BytesIO(b"A" * guard.MAX_STREAM)
    with pytest.raises(guard.RefusalError) as refusal:
        guard.drain_secrets(stream)
    assert "longer" not in str(refusal.value)
    stream = io.BytesIO(b"A" * (guard.MAX_STREAM + 4))
    with pytest.raises(guard.RefusalError, match="longer"):
        guard.drain_secrets(stream)


def test_drain_reads_to_the_end_of_a_stream_that_arrives_in_pieces() -> None:
    data = base64.b64encode(_tar(KEYS))
    reader, writer = os.pipe()

    def _write() -> None:
        with os.fdopen(writer, "wb") as handle:
            for start in range(0, len(data), 7):
                handle.write(data[start : start + 7])
                handle.flush()
                time.sleep(0.001)

    thread = threading.Thread(target=_write)
    thread.start()
    with os.fdopen(reader, "rb") as handle:
        assert guard.drain_secrets(handle) == KEYS
    thread.join(timeout=30)


def test_the_guard_has_no_fifo_or_alarm_code_left() -> None:
    source = GUARD_PATH.read_text(encoding="utf-8")
    for word in ("SIGALRM", "setitimer", "SECRETS_TIMEOUT", "_read_timed"):
        assert word not in source, word


def test_write_keys_leaves_nothing_when_the_second_key_cannot_be_written(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "keys"
    directory.mkdir()
    (directory / "repo-key").write_bytes(b"already there")
    with pytest.raises(FileExistsError):
        guard.write_keys(KEYS, directory)
    assert {path.name for path in directory.iterdir()} == {"repo-key"}
    assert (directory / "repo-key").read_bytes() == b"already there"


def test_a_job_without_a_keys_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _Job(tmp_path)
    job.keys_dir.rmdir()
    job.cache.mkdir(parents=True)
    monkeypatch.setattr(guard, "_run", _Git({}))
    assert job.main(_env()) == 1
    assert "refusing the job" in capsys.readouterr().err


def test_writing_the_keys_is_the_last_act_before_admission() -> None:
    tree = ast.parse(GUARD_PATH.read_text(encoding="utf-8"))
    main = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    attempt = next(node for node in main.body if isinstance(node, ast.Try))
    last = [ast.unparse(statement) for statement in attempt.body[-2:]]
    assert last == ["_check_shim(shim)", "write_keys(keys, keys_dir)"]
    assert ast.unparse(main.body[-1]) == "return 0"
    assert attempt is main.body[-2]


# The shim is the guard's parent, and the guard dies with it.


@pytest.mark.parametrize("argv", [["g"], ["g", "x"], ["g", "1", "2"], ["g", ""]])
def test_refuses_without_the_shims_process_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    job = _Job(tmp_path)
    job.argv = argv
    assert job.main(_env()) == 1
    assert "process ID" in capsys.readouterr().err
    assert job.written() == {}
    # It refuses before it reads, since the shim already holds the keys.
    assert job.stream.tell() == 0


def test_refuses_when_its_parent_is_not_the_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _Job(tmp_path)
    job.cache.mkdir(parents=True)
    git = _Git({})
    monkeypatch.setattr(guard, "_run", git)
    job.argv = ["g", str(os.getpid())]
    assert job.main(_env()) == 1
    assert "the parent is not the shim" in capsys.readouterr().err
    assert git.calls == []
    assert job.written() == {}


def test_checks_its_parent_again_just_before_it_writes_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _Job(tmp_path)
    job.cache.mkdir(parents=True)
    git = _Git({})
    monkeypatch.setattr(guard, "_run", git)
    real = guard._check_shim  # noqa: SLF001
    checks: list[int] = []

    def _second_fails(shim: int) -> None:
        checks.append(shim)
        if len(checks) == 2:  # the check at the start, then the last
            message = "the parent is not the shim (a late exit)"
            raise guard.RefusalError(message)
        real(shim)

    monkeypatch.setattr(guard, "_check_shim", _second_fails)
    assert job.main(_env()) == 1
    assert "a late exit" in capsys.readouterr().err
    assert [call[3] for call in git.calls] == ["fetch", "merge-base"]
    assert job.written() == {}


def test_the_guard_refuses_to_tie_its_life_to_the_shim_off_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fresh module, since the autouse fixture replaced the function in `guard`.
    module = _load()
    monkeypatch.setattr(module, "sys", types.SimpleNamespace(platform="darwin"))
    with pytest.raises(module.RefusalError, match="this platform"):
        module._die_with_parent()  # noqa: SLF001


def test_the_guard_refuses_when_prctl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Native:
        @staticmethod
        def prctl(*args: int) -> int:
            assert args == (1, 9, 0, 0, 0)
            return -1

    module = _load()
    monkeypatch.setattr(module, "sys", types.SimpleNamespace(platform="linux"))

    def native(*_args: object, **_kwargs: object) -> _Native:
        return _Native()

    monkeypatch.setattr(module.ctypes, "CDLL", native)
    with pytest.raises(module.RefusalError, match="PR_SET_PDEATHSIG"):
        module._die_with_parent()  # noqa: SLF001


def test_tying_to_the_shim_requests_the_death_signal_and_then_checks() -> None:
    module = _load()
    calls: list[str] = []

    def die() -> None:
        calls.append("die")

    def check(shim: int) -> None:
        calls.append(f"check {shim}")

    module.__dict__["_die_with_parent"] = die
    module.__dict__["_check_shim"] = check
    module._tie_to_shim(42)  # noqa: SLF001
    assert calls == ["die", "check 42"]


@LINUX_ONLY
def test_the_death_signal_is_sigkill_after_the_guard_asks_for_it() -> None:
    code = (
        "import ctypes, sys\n"
        "from importlib.machinery import SourceFileLoader\n"
        "from importlib.util import module_from_spec, spec_from_loader\n"
        f"loader = SourceFileLoader('g', {str(GUARD_PATH)!r})\n"
        "guard = module_from_spec(spec_from_loader('g', loader))\n"
        "loader.exec_module(guard)\n"
        "guard._die_with_parent()\n"
        "value = ctypes.c_int(0)\n"
        "assert ctypes.CDLL(None).prctl(2, ctypes.byref(value)) == 0\n"
        "sys.stdout.write(str(value.value))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "9"


# The fetch key's memory file, and git's process group.


@LINUX_ONLY
def test_the_fetch_key_sits_in_a_memory_file_only_the_process_reaches() -> None:

    key = b"DUMMY KEY\n" * 100_000
    with guard._memory_key(key) as path:  # noqa: SLF001
        assert re.fullmatch(rf"/proc/{os.getpid()}/fd/\d+", str(path))
        assert path.read_bytes() == key
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        descriptor = int(path.name)
        posix_fcntl = importlib.import_module("fcntl")  # POSIX only; skipped elsewhere
        flags = posix_fcntl.fcntl(descriptor, posix_fcntl.F_GETFD)
        assert flags & posix_fcntl.FD_CLOEXEC
        # A child doesn't inherit it.
        listing = subprocess.run(
            ["ls", "/proc/self/fd"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert str(descriptor) not in listing.stdout.split()
    assert not path.exists()


@LINUX_ONLY
def test_the_fetch_key_closes_when_the_block_raises() -> None:
    paths: list[Path] = []

    def _use() -> None:
        with guard._memory_key(b"x") as path:  # noqa: SLF001
            paths.append(path)
            message = "stop"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="stop"):
        _use()
    assert not paths[0].exists()


def test_the_fetch_key_refuses_where_there_is_no_memfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setattr(module, "sys", types.SimpleNamespace(platform="darwin"))
    with (
        pytest.raises(module.RefusalError, match="memfd_create"),
        module._memory_key(  # noqa: SLF001
            b"x"
        ),
    ):
        pytest.fail("the block must not run")


def test_run_captures_the_output_and_status() -> None:
    result = guard._run(  # noqa: SLF001
        ["sh", "-c", "echo out; echo err >&2; exit 3"], {"PATH": os.environ["PATH"]}
    )
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def _gone(pid: int, wait: float = 10.0) -> bool:
    """Whether the process has exited, in the time given; a zombie has."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if sys.platform == "linux":
            try:
                state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            except OSError:
                return True
            if state.rsplit(")", 1)[1].split()[0] == "Z":
                return True
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        time.sleep(0.02)
    return False


def test_run_kills_the_whole_group_when_git_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "GIT_TIMEOUT", 1.0)
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        guard._run(  # noqa: SLF001
            ["sh", "-c", 'sleep 300 & echo $! >"$1"; wait', "sh", str(pidfile)],
            {"PATH": os.environ["PATH"]},
        )
    assert time.monotonic() - started < 30
    assert _gone(int(pidfile.read_text(encoding="utf-8")))


def test_run_kills_what_git_leaves_behind_when_it_exits(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    result = guard._run(  # noqa: SLF001
        [
            "sh",
            "-c",
            '(sleep 300 >/dev/null 2>&1 & echo $! >"$1"); exit 0',
            "sh",
            str(pidfile),
        ],
        {"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0
    assert _gone(int(pidfile.read_text(encoding="utf-8")))


def test_run_has_no_stdin_and_its_own_session() -> None:
    code = (
        "import os, sys; sys.stdin.read(); "
        "print(os.getsid(0), os.getpgid(0), os.getpid())"
    )
    result = guard._run(  # noqa: SLF001
        [sys.executable, "-I", "-c", code], {"PATH": os.environ["PATH"]}
    )
    assert result.returncode == 0, result.stderr
    session, group, pid = result.stdout.split()
    # The child leads a session and a group of its own, apart from this test's.
    assert session == group == pid
    assert int(session) != POSIX_OS.getsid(0)


# The slot script's writer, run for real.


def _writer_line() -> str:
    script = SLOT_PATH.read_text(encoding="utf-8")
    return next(row for row in script.splitlines() if row.strip().startswith("{ tar "))


def test_the_slot_scripts_writer_is_tar_into_base64_into_the_fifo() -> None:
    assert _writer_line().strip() == (
        '{ tar --create --file - --directory "$ACTIONS_RUNNER_POOL_JOB_SECRETS_DIR"'
        ' -- . | base64; } >"$secrets_fifo" &'
    )


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not Path("/bin/bash").exists(),
    reason="the writer needs a FIFO and bash",
)
def test_the_slot_scripts_own_writer_makes_a_stream_the_guard_reads(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    for name, data in {**KEYS, "deploy-key.pub": b"public\n"}.items():
        (source / name).write_bytes(data)
    fifo = tmp_path / "fifo"
    POSIX_OS.mkfifo(fifo)
    # The writer starts first and blocks in its open, as in the slot script.
    writer = subprocess.Popen(
        ["/bin/bash", "-c", f"set -euo pipefail\n{_writer_line()}\nwait"],
        env={
            **os.environ,
            "ACTIONS_RUNNER_POOL_JOB_SECRETS_DIR": str(source),
            "secrets_fifo": str(fifo),
        },
    )
    try:
        time.sleep(0.2)
        assert writer.poll() is None
        with fifo.open("rb") as handle:
            lines = handle.read().splitlines()
        assert writer.wait(timeout=30) == 0
    finally:
        writer.kill()
        writer.wait()
    # The shim joins the lines, and the guard reads that.
    assert guard.drain_secrets(io.BytesIO(b"".join(lines))) == KEYS
