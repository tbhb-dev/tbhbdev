# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""GitHub App command output, vault reads and dispatch."""

import errno
import inspect
import os
from dataclasses import replace
lazy from pathlib import Path
lazy from types import ModuleType

import httpx2
import pytest
from integration.alpha.gh_app_fakes import (
    APP_VAULT,
    NOW,
    REPO,
    TOKEN,
    Github,
    Key,
    Store,
    app_store,
    env_for,
    mode,
    ok_routes,
    out_path,
    run,
    seed,
)

from tbhbdev import secrets
from tbhbdev.alpha import (
    gh_app_api,
    gh_app_args,
    gh_app_command,
    gh_app_runtime,
    gh_app_signing,
    gh_app_state,
)

pytestmark = pytest.mark.usefixtures("no_network")

# The names of the 1Password service account's variable, command and token
# file, split for gitleaks.
BANNED = ("OP_SERVICE_" + "ACCOUNT", "op " + "read", "service" + "-account")


@pytest.mark.parametrize(
    "argv", [["-h"], ["--help"], ["bot", REPO, "-h"], ["bot", "--help", "--fresh"]]
)
def test_help(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    assert gh_app_command.main(argv, "p") == 0
    assert capsys.readouterr() == (gh_app_args.USAGE.format(prog="p"), "")
    with pytest.raises(gh_app_args.HelpRequestedError):
        gh_app_args.parse_args(argv)


def test_a_usage_error_prints_usage_and_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store, github = app_store("k"), Github()
    env = env_for(tmp_path, store, github)
    code, out, err = run(capsys, env, "Bad_App", REPO)
    assert (code, out) == (2, "")
    assert err.startswith("gh-app: app 'Bad_App' must match ")
    assert err.endswith(gh_app_args.USAGE.format(prog="gh-app"))
    assert (store.created, github.requests) == (0, [])
    assert run(capsys, env, "bot")[0] == 2
    assert run(capsys, env, "bot", "nope")[0] == 2


def test_default_main_builds_its_own_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert gh_app_command.main(["bot", "--cooldown-status"], "gh-app") == 0
    assert capsys.readouterr() == ("no cooldown\n", "")


def test_one_store_serves_both_fields_of_a_mint(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store = app_store(rsa.pem)
    assert run(capsys, env_for(tmp_path, store, Github()), "bot", REPO)[0] == 0
    assert store.created == 1
    assert len(store.reads) == 2


def test_a_store_that_cannot_be_built_is_an_unreadable_field(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def broken() -> Store:
        message = "no token"
        raise secrets.SecretsError(message)

    env = replace(env_for(tmp_path, app_store("k"), Github()), store=broken)
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out) == (1, "")
    assert err == "cannot read app_id from tbhb-dev/github-app-bot\n"


@pytest.mark.parametrize("missing", ["app_id", "private_key"])
@pytest.mark.parametrize("how", ["absent", "empty", "blank"])
def test_an_unreadable_field_is_an_error_that_names_no_value(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    missing: str,
    how: str,
) -> None:
    store = app_store(rsa.pem, vault=APP_VAULT)
    ref = f"op://{APP_VAULT}/github-app-bot/{missing}"
    if how == "absent":
        del store.values[ref]
    else:
        store.values[ref] = "" if how == "empty" else " \n"
    github = Github()
    code, out, err = run(
        capsys, env_for(tmp_path, store, github), "bot", REPO, "--vault", APP_VAULT
    )
    assert (code, out) == (1, "")
    assert err == f"cannot read {missing} from {APP_VAULT}/github-app-bot\n"
    assert github.requests == []
    assert not out_path(tmp_path, vault=APP_VAULT).exists()


def test_the_vault_can_be_overridden(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store = app_store(rsa.pem, vault="other")
    env = env_for(tmp_path, store, Github())
    assert run(capsys, env, "bot", REPO, "--vault", "other")[0] == 0
    assert store.reads[0] == "op://other/github-app-bot/app_id"
    env = env_for(tmp_path, store, Github())
    code, _, err = run(capsys, env, "bot", REPO, "--fresh")
    assert (code, err) == (1, "cannot read app_id from tbhb-dev/github-app-bot\n")


def test_nothing_secret_reaches_the_output(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    store = app_store(rsa.pem)
    seen = ""
    extras: list[list[str]] = [[], ["--dry-run"], ["--all-repos"]]
    for extra in extras:
        code, out, err = run(
            capsys, env_for(tmp_path, store, github), "bot", REPO, *extra
        )
        assert code == 0
        seen += out + err
    failing = Github(
        {
            **ok_routes(),
            ("POST", "/app/installations/7/access_tokens"): httpx2.Response(
                401, json={"message": "no"}
            ),
        }
    )
    seen += "".join(
        run(capsys, env_for(tmp_path, store, failing), "bot", REPO, "--fresh")[1:]
    )
    seen += "".join(
        run(
            capsys, env_for(tmp_path, app_store("junk"), github), "bot", REPO, "--fresh"
        )[1:]
    )
    for secret in (
        TOKEN,
        rsa.body[:40],
        rsa.body[-40:],
        github.jwt,
        github.jwt.split(".")[2],
    ):
        assert secret not in seen
    assert "BEGIN" not in seen


def test_an_error_with_no_file_name_prints_its_reason_alone(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail(_descriptor: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", fail)
    env = env_for(tmp_path, app_store("k"), Github())
    assert run(capsys, env, "bot", "--cooldown-until", "5") == (
        1,
        "",
        "Input/output error\n",
    )


def test_cooldown_until_writes_one_epoch_atomically(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store, github = app_store("k"), Github()
    env = env_for(tmp_path, store, github)
    path = tmp_path / "state" / "cooldown" / "bot"
    assert run(capsys, env, "bot", "--cooldown-until", str(NOW + 5)) == (0, "", "")
    assert path.read_text() == f"{NOW + 5}\n"
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert run(capsys, env, "bot", "--cooldown-until", "7")[0] == 0
    assert path.read_text() == "7\n"
    assert [p.name for p in path.parent.iterdir()] == ["bot"]
    assert (store.created, github.requests) == (0, [])


def test_cooldown_status(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    env = env_for(tmp_path, app_store("k"), Github())
    assert run(capsys, env, "bot", "--cooldown-status") == (0, "no cooldown\n", "")
    run(capsys, env, "bot", "--cooldown-until", str(NOW + 1))
    assert run(capsys, env, "bot", "--cooldown-status") == (
        75,
        f"cooldown until {gh_app_state.utc(NOW + 1)}\n",
        "",
    )
    run(capsys, env, "bot", "--cooldown-until", str(NOW))
    assert run(capsys, env, "bot", "--cooldown-status") == (0, "no cooldown\n", "")


def test_dry_run_signs_without_a_request_or_state(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store, github = app_store(rsa.pem), Github()
    out_dir = seed(tmp_path, f"{NOW + 9999}".encode())
    code, out, err = run(
        capsys, env_for(tmp_path, store, github), "bot", REPO, "--dry-run"
    )
    assert (code, err) == (0, "")
    assert out == (
        "dry run: read app_id and private_key for bot, "
        "signed a JWT and made no request\n"
    )
    assert github.requests == []
    assert len(store.reads) == 2
    assert (out_dir / "hosts.yml").read_text() == "old\n"
    assert sorted(p.name for p in out_dir.iterdir()) == ["expires-at", "hosts.yml"]


def test_dry_run_still_validates_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env = env_for(tmp_path, app_store("junk"), Github())
    assert run(capsys, env, "bot", REPO, "--dry-run") == (
        1,
        "",
        "private_key does not parse\n",
    )


@pytest.mark.parametrize(
    "module",
    [
        gh_app_api,
        gh_app_args,
        gh_app_command,
        gh_app_runtime,
        gh_app_signing,
        gh_app_state,
    ],
)
def test_the_module_names_no_service_account(module: ModuleType) -> None:
    source = inspect.getsource(module)
    for banned in BANNED:
        assert banned not in source


def test_a_run_never_mints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("TBHBDEV_AGENT_RUN", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert gh_app_command.main(["bot", REPO], "gh-app") == 1
    assert capsys.readouterr() == (
        "",
        "gh-app: refused inside an agent run; a run never mints an App token\n",
    )
    assert not (tmp_path / "gh-app").exists()
