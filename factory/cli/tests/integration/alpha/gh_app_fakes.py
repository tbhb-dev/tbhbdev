# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Shared GitHub App test fixtures and fakes."""

# cspell:words getproxies

import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
lazy from pathlib import Path

import httpx2
lazy import pytest

from tbhbdev import secrets
from tbhbdev.alpha import gh_app_runtime
lazy from tbhbdev.alpha import gh_app_command

NOW = 1_800_000_000
# The token a fake GitHub mints lives an hour past the fake clock.
EXPIRES_EPOCH = NOW + 3600
EXPIRES = datetime.fromtimestamp(EXPIRES_EPOCH, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
# Split so that gitleaks sees neither a GitHub token nor a key block.
TOKEN = "ghs" + "_FakeInstallationToken0123456789"
BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
END = "-----END " + "RSA PRIVATE KEY-----"
APP_ID = "424242"
APP_VAULT = "vault-app"
REPO = "acme/widget"


@dataclass
class Key:
    private: Path
    public: Path

    @property
    def pem(self) -> str:
        return self.private.read_text()

    @property
    def body(self) -> str:
        return "".join(self.pem.splitlines()[1:-1])


@dataclass
class Store:
    values: dict[str, str]
    reads: list[str] = field(default_factory=list)
    created: int = 0

    def factory(self) -> Store:
        self.created += 1
        return self

    def read(self, ref: str, /) -> str:
        self.reads.append(ref)
        if ref not in self.values:
            message = f"{ref!r} is missing"
            raise secrets.SecretsError(message)
        return self.values[ref]


def app_store(key: str, *, app: str = "bot", vault: str = "tbhb-dev") -> Store:
    base = f"op://{vault}/github-app-{app}"
    return Store({f"{base}/app_id": APP_ID, f"{base}/private_key": key})


def ok_routes(installation: int = 7) -> dict[tuple[str, str], httpx2.Response]:
    return {
        ("GET", f"/repos/{REPO}/installation"): httpx2.Response(
            200, json={"id": installation}
        ),
        ("POST", f"/app/installations/{installation}/access_tokens"): httpx2.Response(
            201, json={"token": TOKEN, "expires_at": EXPIRES}
        ),
    }


class Github:
    """The GitHub API: answers each route and 404s the rest."""

    def __init__(
        self, routes: dict[tuple[str, str], httpx2.Response] | None = None
    ) -> None:
        self.routes = ok_routes() if routes is None else routes
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        return self.routes.get(key, httpx2.Response(404, json={"message": "Not Found"}))

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)

    @property
    def jwt(self) -> str:
        return self.requests[0].headers["Authorization"].removeprefix("Bearer ")


def env_for(
    tmp_path: Path,
    store: Store,
    github: Github,
    *,
    now: int = NOW,
    openssl: gh_app_runtime.OpenSsl = gh_app_runtime.run_openssl,
) -> gh_app_runtime.Env:
    return gh_app_runtime.Env(
        tmp_path / "state", lambda: now, store.factory, openssl, github.transport
    )


def run(
    capsys: pytest.CaptureFixture[str], env: gh_app_runtime.Env, *argv: str
) -> tuple[int, str, str]:
    code = gh_app_command.main(list(argv), "gh-app", env=env)
    out, err = capsys.readouterr()
    return code, out, err


def out_path(tmp_path: Path, *parts: str, vault: str = "tbhb-dev") -> Path:
    return tmp_path.joinpath("state", vault, "bot", "acme", *parts)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def seed(tmp_path: Path, expires: bytes | None, *, hosts: bool = True) -> Path:
    out_dir = out_path(tmp_path, "widget")
    out_dir.mkdir(parents=True)
    if hosts:
        (out_dir / "hosts.yml").write_text("old\n")
    if expires is not None:
        (out_dir / "expires-at").write_bytes(expires)
    return out_dir


def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail at once on any request that no MockTransport answers."""
    monkeypatch.delenv(gh_app_runtime.RUN_ENV, raising=False)

    def refuse(*_args: object, **_kwargs: object) -> httpx2.Response:
        message = "a test reached the real network"
        raise AssertionError(message)

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)
    # A client with no transport of its own looks up the host's proxies first,
    # which crashes a forked mutmut worker on macOS before it can send anything.
    monkeypatch.setattr("httpx2._utils.getproxies", refuse)
