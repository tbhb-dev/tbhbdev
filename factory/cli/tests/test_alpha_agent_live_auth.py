"""Live sign-in uses production copying, refresh write-back and removal."""

import dataclasses
import json
import stat
from pathlib import Path

import agent_live_evidence
import pytest
from agent_host_example import make_run

from tbhbdev.alpha.agent.run_host.paths import planned_paths


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("account_id", [None, "fixture-account"])
def test_live_codex_auth_refresh_is_written_back_and_removed(
    tmp_path: Path, failure: bool, account_id: str | None
) -> None:
    root = tmp_path / "probe"
    run = make_run(root)
    account = tmp_path / "account"
    source = account / ".codex/auth.json"
    source.parent.mkdir(parents=True)
    # Codex 0.164.0 TokenData serializes OAuth provenance as a nested object:
    # https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/login/src/token_data.rs
    tokens = {
        "id_token": "fixture-id",
        "access_token": "fixture-access",
        "refresh_token": "fixture-before",
        "oauth_provenance": {
            "issuer": "https://auth.openai.com",
            "client_id": "fixture-client",
        },
        "login_owner_id": "fixture-owner",
        "account_id": account_id,
    }
    before = json.dumps({"tokens": tokens})
    after = json.dumps({"tokens": tokens | {"refresh_token": "fixture-after"}})
    source.write_text(before)
    paths = dataclasses.replace(planned_paths(run), codex_home=str(root / "codex"))

    def probe() -> None:
        with agent_live_evidence.codex_auth(run, paths, account, root):
            directory = Path(paths.codex_home)
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert (directory / "auth.json").read_text() == before
            (directory / "auth.json").write_text(after)
            if failure:
                message = "fixture probe failed"
                raise RuntimeError(message)

    if failure:
        with pytest.raises(RuntimeError, match="fixture probe failed"):
            probe()
    else:
        probe()
    assert source.read_text() == after
    assert not list(root.rglob("auth.json"))
    assert not Path(paths.codex_home).exists()
