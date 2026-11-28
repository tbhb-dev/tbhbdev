"""Verify installation token caching, refresh, 401 retry, and socket helper."""

from __future__ import annotations

import dataclasses
import io
import socket
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from agent_host_example import host_settings, run_request

from tbhbdev import secrets
from tbhbdev.alpha import gh_app_api, gh_app_runtime
from tbhbdev.alpha.agent import credentials, token_helper
from tbhbdev.alpha.agent.model import Invalid


def _app_settings() -> credentials.Settings:
    base = host_settings()
    creds = dataclasses.replace(
        base.credentials[0],
        gh=None,
        pull_requests_app_client_id="app/id",
        pull_requests_app_pem="app/pem",
    )
    return dataclasses.replace(base, credentials=(creds,))


def test_build_delivery_caches_refreshes_and_serves_git_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="ag-tok-") as directory:
        tmpdir, seen, now, done = Path(directory), [], 1000, threading.Event()
        ref_v, ret_v, pro_v = "refreshed-t", "retried-t", "proactive-t"
        vals: tuple[gh_app_api.Minted | Exception, ...] = (
            gh_app_api.Minted("init-t", 4600),
            gh_app_api.Minted(ref_v, 5200),
            gh_app_api.Minted(ret_v, 4600),
            gh_app_runtime.GhAppError("transient"),
            gh_app_api.Minted(pro_v, 9000),
        )
        seq = iter(vals)

        def mint(_env: gh_app_runtime.Env, **kw: object) -> gh_app_api.Minted:
            seen.append(kw)
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            if item.token == pro_v:
                done.set()
            return item

        monkeypatch.setattr(secrets, "read", "val-{}".format)
        monkeypatch.setattr(gh_app_api, "mint_token", mint)
        monkeypatch.setattr(gh_app_runtime, "clock", lambda: now)
        monkeypatch.setattr(token_helper, "RETRY_INTERVAL", 0.01)
        helper = credentials.deliver(
            run_request(), _app_settings(), tmpdir
        ).token_helper
        assert helper is not None
        assert "init-t" not in repr(helper)
        gh_dir, sock = tmpdir / "gh", tmpdir / token_helper.GH_SOCKET
        monkeypatch.setenv("GH_CONFIG_DIR", str(gh_dir))

        def call(act: str, body: str = "protocol=https\nhost=github.com\n\n") -> str:
            out = io.StringIO()
            monkeypatch.setattr(sys, "stdin", io.StringIO(body))
            monkeypatch.setattr(sys, "stdout", out)
            assert token_helper.git_credential_main([act], "git-credential") == 0
            return out.getvalue()

        with helper.serve(sock, min_wait=0.01):
            assert sock.stat().st_mode & 0o777 == 0o600
            assert ("password=init-t\n" in call("get"), len(seen)) == (True, 1)
            now = 4000
            assert f"password={ref_v}\n" in call("get")
            assert (call("erase"), helper.rejected) == ("", True)
            hosts = yaml.safe_load((gh_dir / "hosts.yml").read_text())["github.com"]
            assert (hosts["oauth_token"], call("store"), helper.rejected) == (
                ret_v,
                "",
                False,
            )
            assert (
                token_helper.send(str(gh_dir), "unknown"),
                call("other"),
                call("get", "protocol=https\nhost=x\n\n"),
            ) == (None, "", "")
            assert done.wait(timeout=2.0)
        assert (not sock.exists(), f"password={pro_v}\n" in call("get")) == (True, True)
        assert all(kw["permissions"] == credentials.PUSH_PERMISSIONS for kw in seen)


def test_token_helper_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory(prefix="ag-tok-") as directory:
        tmpdir = Path(directory)
        gh_dir, sock = tmpdir / "gh", tmpdir / token_helper.GH_SOCKET
        gh_dir.mkdir()
        with socket.socket(socket.AF_UNIX) as dead:
            dead.bind(str(sock))
        minted = gh_app_api.Minted("t", 2000)
        helper = token_helper.TokenHelper(lambda: minted, minted)
        monkeypatch.setenv("GH_CONFIG_DIR", str(gh_dir))
        monkeypatch.setattr(
            sys, "stdin", io.StringIO("protocol=https\nhost=github.com\n\n")
        )
        assert (
            token_helper.send(str(gh_dir), "get"),
            helper.step(dead, threading.Event()),
            token_helper.git_credential_main(["get"], "git-credential"),
        ) == (None, None, 0)
        with pytest.raises(Invalid, match="redirected"):
            credentials.write_hosts(gh_dir, tmpdir, "github.com", "x")
        monkeypatch.setattr(secrets, "read", "val-{}".format)
        monkeypatch.setattr(
            gh_app_api, "mint_token", Mock(side_effect=gh_app_runtime.GhAppError("x"))
        )
        with pytest.raises(Invalid, match="build refused"):
            credentials.fetch_token(run_request(), _app_settings())
