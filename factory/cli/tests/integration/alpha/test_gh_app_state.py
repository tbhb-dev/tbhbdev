# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""GitHub App cache, cooldown and atomic writes."""

# cspell:words EROFS RDONLY

import errno
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from integration.alpha.gh_app_fakes import (
    APP_VAULT,
    EXPIRES,
    EXPIRES_EPOCH,
    NOW,
    REPO,
    TOKEN,
    Github,
    Key,
    app_store,
    env_for,
    mode,
    out_path,
    run,
    seed,
)

from tbhbdev.alpha import (
    gh_app_args,
    gh_app_state,
)

pytestmark = pytest.mark.usefixtures("no_network")


def test_layout() -> None:
    state = Path("/s")
    args = gh_app_args.parse_args(["bot", "o/r"])
    assert gh_app_state.out_dir(state, args) == Path("/s/tbhb-dev/bot/o/r")
    assert gh_app_state.out_dir(state, replace(args, all_repos=True)) == Path(
        "/s/tbhb-dev/bot/o/+all"
    )
    assert gh_app_state.out_dir(state, replace(args, vault="v")) == Path("/s/v/bot/o/r")
    narrowed = replace(
        args, permissions=(("contents", "read"), ("pull_requests", "write"))
    )
    assert gh_app_state.out_dir(state, narrowed) == Path(
        "/s/tbhb-dev/bot/o/r+contents-read+pull-requests-write"
    )
    assert gh_app_state.cooldown_path(state, "bot") == Path("/s/cooldown/bot")
    # GitHub can't name a repository `+all`, so it never shares a directory.
    assert not gh_app_args.REPO_PATTERN.fullmatch("o/+all")


@pytest.mark.parametrize(
    ("text", "until"),
    [
        (None, None),
        ("junk", None),
        ("99", None),
        ("100\n", None),
        ("101\n", 101),
        ("5000", 5000),
    ],
)
def test_cooldown_decision(text: str | None, until: int | None) -> None:
    assert gh_app_state.cooldown_until(text, 100) == until


@pytest.mark.parametrize(
    ("expires", "fresh", "reuse"),
    [
        (str(NOW + 601), False, True),
        (f"{NOW + 601}\n", False, True),
        (str(NOW + 600), False, False),
        (str(NOW + 599), False, False),
        (str(NOW - 5), False, False),
        (str(NOW + 601), True, False),
        ("0", False, False),
        ("junk", False, False),
        (None, False, False),
    ],
)
def test_reuse_decision(expires: str | None, fresh: bool, reuse: bool) -> None:
    assert gh_app_state.reusable(expires, NOW, fresh=fresh) is reuse


def test_utc() -> None:
    assert gh_app_state.utc(0) == "1970-01-01T00:00:00Z"
    assert gh_app_state.utc(EXPIRES_EPOCH) == EXPIRES
    assert gh_app_state.cooldown_message(0) == "cooldown until 1970-01-01T00:00:00Z"


def test_render_hosts() -> None:
    assert gh_app_state.render_hosts("bot", TOKEN) == (
        "github.com:\n"
        "    users:\n"
        '        "bot[bot]":\n'
        f"            oauth_token: {TOKEN}\n"
        "    git_protocol: https\n"
        f"    oauth_token: {TOKEN}\n"
        '    user: "bot[bot]"\n'
    )


def test_a_mint_writes_hosts_yml_and_prints_only_the_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store, github = app_store(rsa.pem, vault=APP_VAULT), Github()
    code, out, err = run(
        capsys, env_for(tmp_path, store, github), "bot", REPO, "--vault", APP_VAULT
    )
    out_dir = out_path(tmp_path, "widget", vault=APP_VAULT)
    assert (code, out, err) == (0, f"{out_dir}\n", "")
    assert (out_dir / "hosts.yml").read_text() == gh_app_state.render_hosts(
        "bot", TOKEN
    )
    assert (out_dir / "app-slug").read_text() == "bot\n"
    assert (out_dir / "expires-at").read_text() == f"{EXPIRES_EPOCH}\n"
    assert mode(out_dir) == 0o700
    for name in ("hosts.yml", "app-slug", "expires-at"):
        assert mode(out_dir / name) == 0o600
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "app-slug",
        "expires-at",
        "hosts.yml",
    ]
    assert store.reads == [
        f"op://{APP_VAULT}/github-app-bot/app_id",
        f"op://{APP_VAULT}/github-app-bot/private_key",
    ]


def test_two_vaults_never_share_a_token(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store = app_store(rsa.pem, vault="a")
    store.values.update(app_store(rsa.pem, vault="b").values)
    github = Github()
    env = env_for(tmp_path, store, github)
    code_a, out_a, _ = run(capsys, env, "bot", REPO, "--vault", "a")
    assert (code_a, out_a) == (0, f"{out_path(tmp_path, 'widget', vault='a')}\n")
    assert len(github.requests) == 2
    # A cached token under `a` is no answer for `b`.
    code_b, out_b, _ = run(capsys, env, "bot", REPO, "--vault", "b")
    assert (code_b, out_b) == (0, f"{out_path(tmp_path, 'widget', vault='b')}\n")
    assert len(github.requests) == 4
    assert run(capsys, env, "bot", REPO, "--vault", "a")[1] == out_a
    assert len(github.requests) == 4


def test_fresh_always_mints_and_cannot_be_reused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github, store = Github(), app_store(rsa.pem)
    env = env_for(tmp_path, store, github)
    out_dir = out_path(tmp_path, "widget")
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert (out_dir / "expires-at").read_text() == f"{EXPIRES_EPOCH}\n"
    assert len(github.requests) == 2
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert len(github.requests) == 2
    assert run(capsys, env, "bot", REPO, "--fresh")[0] == 0
    assert (out_dir / "expires-at").read_text() == "0\n"
    assert len(github.requests) == 4
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert len(github.requests) == 6


def test_a_live_token_is_reused_without_reading_secrets(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    out_dir = seed(tmp_path, f"{NOW + 601}\n".encode())
    store, github = app_store(rsa.pem), Github()
    code, out, err = run(capsys, env_for(tmp_path, store, github), "bot", REPO)
    assert (code, out, err) == (0, f"{out_dir}\n", "")
    assert (store.created, store.reads, github.requests) == (0, [], [])
    assert (out_dir / "hosts.yml").read_text() == "old\n"


@pytest.mark.parametrize(
    ("expires", "hosts"),
    [
        (str(NOW + 600).encode(), True),
        (str(NOW + 599).encode(), True),
        (b"junk", True),
        (b"\xff\xfe", True),
        (None, True),
        (str(NOW + 601).encode(), False),
    ],
)
def test_a_token_that_cannot_be_reused_is_minted_again(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    expires: bytes | None,
    hosts: bool,
) -> None:
    out_dir = seed(tmp_path, expires, hosts=hosts)
    github = Github()
    code, out, _ = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO
    )
    assert (code, out) == (0, f"{out_dir}\n")
    assert len(github.requests) == 2
    assert (out_dir / "hosts.yml").read_text() == gh_app_state.render_hosts(
        "bot", TOKEN
    )


def test_the_state_file_is_replaced_atomically(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa: Key,
) -> None:
    out_dir = seed(tmp_path, b"0")
    replaced: list[tuple[str, str, str]] = []
    real = Path.replace

    def spy(self: Path, target: Path) -> Path:
        replaced.append((self.name, Path(target).name, self.read_text()[:11]))
        assert mode(self) == 0o600
        assert self.parent == Path(target).parent
        if self.name.startswith(".hosts.yml."):
            assert (out_dir / "hosts.yml").read_text() == "old\n"
        return real(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    code, _, _ = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), Github()), "bot", REPO
    )
    assert code == 0
    assert [(a.split(".")[1], b) for a, b, _ in replaced] == [
        ("hosts", "hosts.yml"),
        ("app-slug", "app-slug"),
        ("expires-at", "expires-at"),
    ]
    assert replaced[0][2] == "github.com:"


def test_each_file_is_synced_before_and_its_directory_after_the_rename(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa: Key,
) -> None:
    out_dir = seed(tmp_path, b"0")
    events: list[str] = []
    synced: list[int] = []
    real_fsync, real_replace = os.fsync, Path.replace

    def fsync(descriptor: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(f"fsync {kind}")
        synced.append(descriptor)
        real_fsync(descriptor)

    def replace(self: Path, target: Path) -> Path:
        events.append(f"replace {Path(target).name}")
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    env = env_for(tmp_path, app_store(rsa.pem), Github())
    assert run(capsys, env, "bot", REPO)[0] == 0
    # The first sync is the key file's, which no rename follows.
    writes = events[events.index("replace hosts.yml") - 1 :]
    assert writes == [
        "fsync file",
        "replace hosts.yml",
        "fsync dir",
        "fsync file",
        "replace app-slug",
        "fsync dir",
        "fsync file",
        "replace expires-at",
        "fsync dir",
    ]
    assert (out_dir / "expires-at").read_text() == f"{EXPIRES_EPOCH}\n"
    for descriptor in synced:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


def test_a_directory_sync_failure_still_closes_the_directory(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opened: list[int] = []
    real_open, real_fsync = os.open, os.fsync

    def spy_open(path: Path, flags: int, *rest: int) -> int:
        descriptor = real_open(path, flags, *rest)
        if flags == os.O_RDONLY:
            opened.append(descriptor)
        return descriptor

    def fsync(descriptor: int) -> None:
        if descriptor in opened:
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", fsync)
    env = env_for(tmp_path, app_store("k"), Github())
    code, _, err = run(capsys, env, "bot", "--cooldown-until", "5")
    assert (code, err) == (1, "Input/output error\n")
    assert len(opened) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])
    leftovers = [p.name for p in (tmp_path / "state" / "cooldown").iterdir()]
    assert leftovers == ["bot"]


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa: Key,
) -> None:
    out_dir = seed(tmp_path, b"0")

    def fail(_self: Path, target: Path) -> Path:
        raise OSError(28, "No space left on device", str(target))

    monkeypatch.setattr(Path, "replace", fail)
    env = env_for(tmp_path, app_store(rsa.pem), Github())
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out) == (1, "")
    assert err == f"cannot write {out_dir / 'hosts.yml'}: No space left on device\n"
    assert (out_dir / "hosts.yml").read_text() == "old\n"
    assert sorted(p.name for p in out_dir.iterdir()) == ["expires-at", "hosts.yml"]
    assert TOKEN not in err


def test_the_original_error_survives_a_temp_file_that_is_already_gone(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def vanish(self: Path, target: Path) -> Path:
        self.unlink()
        raise OSError(errno.EROFS, "Read-only file system", str(target))

    monkeypatch.setattr(Path, "replace", vanish)
    env = env_for(tmp_path, app_store("k"), Github())
    code, _, err = run(capsys, env, "bot", "--cooldown-until", "5")
    path = tmp_path / "state" / "cooldown" / "bot"
    assert (code, err) == (1, f"cannot write {path}: Read-only file system\n")


def test_a_cooldown_serves_a_cached_token_and_refuses_any_other_mint(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store, github = app_store(rsa.pem), Github()
    env = env_for(tmp_path, store, github)
    out_dir = seed(tmp_path, f"{NOW + 601}".encode())
    run(capsys, env, "bot", "--cooldown-until", str(NOW + 1))
    # Reuse calls neither the vault nor GitHub, so the cooldown lets it through.
    assert run(capsys, env, "bot", REPO) == (0, f"{out_dir}\n", "")
    assert (store.created, store.reads, github.requests) == (0, [], [])
    # Every other mint, a --fresh one included, stops before any read.
    refused = f"cooldown until {gh_app_state.utc(NOW + 1)}\n"
    extras: list[list[str]] = [["--fresh"], ["--dry-run"], ["--all-repos"]]
    for extra in extras:
        assert run(capsys, env, "bot", REPO, *extra) == (75, "", refused)
    # A cached token with 600 s left or fewer is no answer either.
    (out_dir / "expires-at").write_text(f"{NOW + 600}\n")
    assert run(capsys, env, "bot", REPO) == (75, "", refused)
    assert (store.created, store.reads, github.requests) == (0, [], [])
    assert (out_dir / "hosts.yml").read_text() == "old\n"


def test_a_cooldown_belongs_to_one_app(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    env = env_for(tmp_path, app_store(rsa.pem, app="other"), Github())
    run(capsys, env, "bot", "--cooldown-until", str(NOW + 1))
    assert run(capsys, env, "other", REPO, "--dry-run")[0] == 0


@pytest.mark.parametrize("until", [NOW, NOW - 1, 0])
def test_an_expired_cooldown_lets_a_mint_through(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key, until: int
) -> None:
    env = env_for(tmp_path, app_store(rsa.pem), Github())
    run(capsys, env, "bot", "--cooldown-until", str(until))
    assert run(capsys, env, "bot", REPO)[0] == 0
