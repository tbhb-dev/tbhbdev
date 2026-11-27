# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""GitHub App installation lookup and token exchange."""

import base64
import json
import os
import ssl
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from typing import NoReturn
from unittest.mock import Mock
lazy from pathlib import Path

import certifi
import httpx2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa as rsa_keys
from integration.alpha.gh_app_fakes import (
    APP_ID,
    EXPIRES,
    EXPIRES_EPOCH,
    NOW,
    REPO,
    TOKEN,
    Github,
    Key,
    Store,
    app_store,
    block_network,
    env_for,
    ok_routes,
    out_path,
    run,
)

from tbhbdev.alpha import (
    gh_app_api,
    gh_app_runtime,
    gh_app_signing,
)

_disable_network = pytest.fixture(autouse=True)(block_network)


def test_request_paths_and_bodies() -> None:
    assert gh_app_api.lookup_paths("o/r") == [
        "/repos/o/r/installation",
        "/orgs/o/installation",
    ]
    assert gh_app_api.token_body("o/r", all_repos=False) == {"repositories": ["r"]}
    assert gh_app_api.token_body("o/r", all_repos=True) == {}
    assert gh_app_api.lookup_paths("a/b/c")[1] == "/orgs/a/installation"
    assert gh_app_api.token_body("a/b/c", all_repos=False) == {"repositories": ["b/c"]}


@pytest.mark.parametrize(
    ("response", "ident"),
    [
        (httpx2.Response(200, json={"id": 7}), 7),
        (httpx2.Response(200, json={"id": "7"}), None),
        (httpx2.Response(200, json={}), None),
        (httpx2.Response(200, json=[7]), None),
        (httpx2.Response(200, content=b"not json"), None),
        (httpx2.Response(404, json={"id": 7}), None),
        (httpx2.Response(201, json={"id": 7}), None),
    ],
)
def test_installation_id(response: httpx2.Response, ident: int | None) -> None:
    assert gh_app_api.installation_id(response) == ident


@pytest.mark.parametrize(
    "expires",
    ["2026-11-09T11:00:00Z", "2026-11-09T11:00:00+00:00", "2026-11-09T06:00:00-05:00"],
)
def test_parse_token(expires: str) -> None:
    minted = gh_app_api.parse_token({"token": TOKEN, "expires_at": expires})
    assert minted.token == TOKEN
    assert minted.expires_at == int(datetime(2026, 11, 9, 11, tzinfo=UTC).timestamp())


def test_a_minted_token_stays_out_of_its_repr() -> None:
    assert TOKEN not in repr(gh_app_api.Minted(TOKEN, 5))
    assert repr(gh_app_api.Minted(TOKEN, 5)) == "Minted(expires_at=5)"


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"message": "Bad credentials", "token": "x y"}, "Bad credentials"),
        (
            {"message": "Bad credentials", "documentation_url": "https://x"},
            "Bad credentials",
        ),
        ({"token": TOKEN}, "no message"),
        ({"expires_at": EXPIRES}, "no message"),
        ({"token": 5, "expires_at": EXPIRES, "message": ""}, "no message"),
        ({"token": TOKEN, "expires_at": 5, "message": 5}, "no message"),
        ({"token": TOKEN, "expires_at": "tomorrow"}, "no message"),
        ({"token": TOKEN, "expires_at": "2026-11-09T11:00:00"}, "no message"),
        ({"token": "bad token\nx", "expires_at": EXPIRES}, "no message"),
        ({"token": "", "expires_at": EXPIRES}, "no message"),
        ({"token": "a:b", "expires_at": EXPIRES}, "no message"),
        ({}, "no message"),
    ],
)
def test_parse_token_quotes_only_the_message_from_github(
    payload: gh_app_api.Json, detail: str
) -> None:
    with pytest.raises(gh_app_runtime.GhAppError) as caught:
        gh_app_api.parse_token(payload)
    assert str(caught.value) == f"GitHub did not return a token: {detail}"


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"message": "Bad credentials"}, "Bad credentials"),
        ({"message": f"bad token {TOKEN} here"}, "bad token [redacted] here"),
        ({"message": ""}, "no message"),
        ({"message": 5}, "no message"),
        ({}, "no message"),
    ],
)
def test_detail(payload: gh_app_api.Json, text: str) -> None:
    assert gh_app_api.detail(payload) == text


def test_a_mint_makes_the_documented_requests(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    run(capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO)
    lookup, create = github.requests
    assert (lookup.method, str(lookup.url)) == (
        "GET",
        "https://api.github.com/repos/acme/widget/installation",
    )
    assert (create.method, str(create.url)) == (
        "POST",
        "https://api.github.com/app/installations/7/access_tokens",
    )
    assert json.loads(create.content) == {"repositories": ["widget"]}
    for request in (lookup, create):
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.headers["Authorization"] == f"Bearer {github.jwt}"
        names = {name for name, _ in request.headers.raw}
        assert {b"Accept", b"X-GitHub-Api-Version", b"Authorization"} <= names
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }


def test_the_installation_falls_back_from_the_repo_to_the_org(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github(
        {
            ("GET", "/orgs/acme/installation"): httpx2.Response(200, json={"id": 9}),
            **{k: v for k, v in ok_routes(9).items() if k[0] == "POST"},
        }
    )
    code, _, _ = run(capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO)
    assert code == 0
    assert [(r.method, r.url.path) for r in github.requests] == [
        ("GET", "/repos/acme/widget/installation"),
        ("GET", "/orgs/acme/installation"),
        ("POST", "/app/installations/9/access_tokens"),
    ]


def lookup_routes(
    repo: httpx2.Response, org: httpx2.Response | None = None
) -> dict[tuple[str, str], httpx2.Response]:
    routes = {("GET", f"/repos/{REPO}/installation"): repo}
    if org is not None:
        routes[("GET", "/orgs/acme/installation")] = org
    return routes


@pytest.mark.parametrize(
    "case",
    [
        (
            httpx2.Response(401, json={"message": "Bad credentials", "x": TOKEN}),
            "401: Bad credentials",
        ),
        (httpx2.Response(403, json={"message": "Rate limited"}), "403: Rate limited"),
        (
            httpx2.Response(301, json={"message": "Moved Permanently"}),
            "301: Moved Permanently",
        ),
        (httpx2.Response(500, content=b"<html>oops"), "500: no message"),
        (httpx2.Response(429, json={"message": ""}), "429: no message"),
        (httpx2.Response(200, json={"id": "7"}), "200: no message"),
        (httpx2.Response(200, content=b"junk"), "200: no message"),
    ],
)
@pytest.mark.parametrize("where", ["repo", "org"])
def test_any_lookup_failure_but_a_404_is_an_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    case: tuple[httpx2.Response, str],
    where: str,
) -> None:
    reply, expected = case
    routes = (
        lookup_routes(reply)
        if where == "repo"
        else lookup_routes(httpx2.Response(404, json={"message": "Not Found"}), reply)
    )
    github = Github(routes)
    code, out, err = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO
    )
    assert (code, out) == (1, "")
    assert err == f"the installation lookup failed with status {expected}\n"
    assert [r.method for r in github.requests] == ["GET"] * (
        1 if where == "repo" else 2
    )
    assert not out_path(tmp_path).exists()
    assert TOKEN not in err


def test_no_installation_is_an_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github({})
    code, out, err = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO
    )
    assert (code, out) == (1, "")
    assert err == "app bot has no installation for acme/widget or acme\n"
    assert [r.method for r in github.requests] == ["GET", "GET"]
    assert not out_path(tmp_path).exists()


def test_all_repos_asks_for_every_repo_and_uses_its_own_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    code, out, _ = run(
        capsys,
        env_for(tmp_path, app_store(rsa.pem), github),
        "bot",
        REPO,
        "--all-repos",
    )
    assert code == 0
    assert out == f"{out_path(tmp_path, '+all')}\n"
    assert json.loads(github.requests[-1].content) == {}
    assert (out_path(tmp_path, "+all") / "hosts.yml").is_file()
    assert not out_path(tmp_path, "widget").exists()


def test_permissions_narrow_the_token_request_and_use_their_own_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    code, out, _ = run(
        capsys,
        env_for(tmp_path, app_store(rsa.pem), github),
        "bot",
        REPO,
        "--permissions",
        "contents:read",
    )
    assert code == 0
    assert out == f"{out_path(tmp_path, 'widget+contents-read')}\n"
    assert json.loads(github.requests[-1].content) == {
        "repositories": ["widget"],
        "permissions": {"contents": "read"},
    }
    assert not out_path(tmp_path, "widget").exists()


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (
            httpx2.Response(401, json={"message": "Bad credentials", "token": "no"}),
            "Bad credentials",
        ),
        (httpx2.Response(201, json={"token": TOKEN}), "no message"),
        (httpx2.Response(500, content=b"<html>oops"), "no message"),
    ],
)
def test_a_bad_token_reply_quotes_only_the_message(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    reply: httpx2.Response,
    detail: str,
) -> None:
    github = Github(
        {**ok_routes(), ("POST", "/app/installations/7/access_tokens"): reply}
    )
    code, out, err = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO
    )
    assert (code, out) == (1, "")
    assert err == f"GitHub did not return a token: {detail}\n"
    assert not out_path(tmp_path).exists()


def test_a_network_failure_names_only_its_type(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        message = f"refused {request.headers['Authorization']}"
        raise httpx2.ConnectError(message)

    env = gh_app_runtime.Env(
        tmp_path / "state",
        lambda: NOW,
        app_store(rsa.pem).factory,
        gh_app_runtime.run_openssl,
        httpx2.MockTransport(boom),
    )
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out) == (1, "")
    assert err == "the GitHub request failed: ConnectError\n"


MINT_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


def test_mint_token_client_uses_only_public_roots(
    tmp_path: Path, rsa: Key, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Mock(wraps=httpx2.Client)
    monkeypatch.setattr(httpx2, "Client", client)
    github = Github()
    assert gh_app_api.mint_token(
        env_for(tmp_path, Store({}), github),
        app_id=APP_ID,
        pem=rsa.pem,
        repository=REPO,
        permissions=MINT_PERMISSIONS,
    ) == gh_app_api.Minted(TOKEN, EXPIRES_EPOCH)
    assert len(github.requests) == 2
    client.assert_called_once()
    assert client.call_args.kwargs["trust_env"] is False
    context: object = client.call_args.kwargs["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    expected = ssl.create_default_context(cafile=certifi.where())
    assert set(context.get_ca_certs(binary_form=True)) == set(
        expected.get_ca_certs(binary_form=True)
    )


def test_mint_token_ignores_proxy_environment(
    tmp_path: Path, rsa: Key, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:bad")
    monkeypatch.setattr(
        "httpx2._utils.getproxies", lambda: {"https": os.environ["HTTPS_PROXY"]}
    )
    github = Github()

    def handle_request(
        _self: httpx2.HTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        return github(request)

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", handle_request)
    assert gh_app_api.mint_token(
        replace(env_for(tmp_path, Store({}), github), transport=None),
        app_id=APP_ID,
        pem=rsa.pem,
        repository=REPO,
        permissions=MINT_PERMISSIONS,
    ) == gh_app_api.Minted(TOKEN, EXPIRES_EPOCH)
    assert len(github.requests) == 2


@pytest.mark.parametrize("permissions", [MINT_PERMISSIONS, {"contents": "read"}])
def test_mint_token_narrows_and_signs_without_files(
    tmp_path: Path,
    rsa: Key,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    permissions: dict[str, str],
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("mint_token must not write files or invoke openssl")

    load_key = serialization.load_pem_private_key

    def load(data: bytes, *, password: bytes | None) -> rsa_keys.RSAPrivateKey:
        assert password is None
        key = load_key(data, password=password)
        assert isinstance(key, rsa_keys.RSAPrivateKey)
        return key

    monkeypatch.setattr(serialization, "load_pem_private_key", load)
    monkeypatch.setattr(tempfile, "mkstemp", refuse)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", refuse)
    before = list(tmp_path.rglob("*"))
    github = Github()
    env = env_for(tmp_path, Store({}), github, openssl=refuse)
    minted = gh_app_api.mint_token(
        env,
        app_id=APP_ID,
        pem=rsa.pem.replace("\n", " "),
        repository=REPO,
        permissions=permissions,
    )
    assert minted == gh_app_api.Minted(TOKEN, EXPIRES_EPOCH)
    assert TOKEN not in repr(minted)
    assert [(r.method, r.url.path) for r in github.requests] == [
        ("GET", f"/repos/{REPO}/installation"),
        ("POST", "/app/installations/7/access_tokens"),
    ]
    assert json.loads(github.requests[-1].content) == {
        "repositories": ["widget"],
        "permissions": permissions,
    }
    header, payload, signature = github.jwt.split(".")
    assert json.loads(base64.urlsafe_b64decode(header + "==")) == gh_app_signing.HEADER
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims == {"iat": NOW - 60, "exp": NOW + 540, "iss": APP_ID}
    assert 0 < claims["exp"] - claims["iat"] <= 600
    public = serialization.load_pem_public_key(rsa.public.read_bytes())
    assert isinstance(public, rsa_keys.RSAPublicKey)
    public.verify(
        base64.urlsafe_b64decode(signature + "=="),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert capsys.readouterr() == ("", "")
    assert list(tmp_path.rglob("*")) == before
    assert all(TOKEN not in p.read_text() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize("status", [404, 401, 403, 500])
def test_mint_token_lookup_falls_back_only_on_404(
    tmp_path: Path,
    rsa: Key,
    status: int,
) -> None:
    routes = ok_routes()
    routes[("GET", f"/repos/{REPO}/installation")] = httpx2.Response(
        status,
        json={"message": TOKEN},
    )
    routes[("GET", "/orgs/acme/installation")] = httpx2.Response(200, json={"id": 7})
    github = Github(routes)
    env = env_for(tmp_path, Store({}), github)
    if status == 404:
        assert gh_app_api.mint_token(
            env,
            app_id=APP_ID,
            pem=rsa.pem,
            repository=REPO,
            permissions=MINT_PERMISSIONS,
        ) == gh_app_api.Minted(TOKEN, EXPIRES_EPOCH)
        assert [r.url.path for r in github.requests] == [
            f"/repos/{REPO}/installation",
            "/orgs/acme/installation",
            "/app/installations/7/access_tokens",
        ]
    else:
        with pytest.raises(gh_app_runtime.GhAppError) as caught:
            gh_app_api.mint_token(
                env,
                app_id=APP_ID,
                pem=rsa.pem,
                repository=REPO,
                permissions=MINT_PERMISSIONS,
            )
        assert str(caught.value) == "GitHub App token mint failed"
        assert len(github.requests) == 1


@pytest.mark.parametrize("permissions", [{}, {"contents": "admin"}, {"contents": ""}])
def test_mint_token_refuses_permissions_before_request(
    tmp_path: Path,
    permissions: dict[str, str],
) -> None:
    github = Github()
    with pytest.raises(gh_app_runtime.GhAppError, match="permissions") as caught:
        gh_app_api.mint_token(
            env_for(tmp_path, Store({}), github),
            app_id=APP_ID,
            pem="invalid key",
            repository=REPO,
            permissions=permissions,
        )
    assert str(caught.value) == (
        "permissions must be nonempty and contain only read or write"
    )
    assert github.requests == []


@pytest.mark.parametrize("kind", ["ec", "invalid"])
def test_mint_token_refuses_keys_without_disclosing_them(
    tmp_path: Path,
    ec_key: Key,
    kind: str,
) -> None:
    pem = ec_key.pem if kind == "ec" else "invalid secret key"
    github = Github()
    with pytest.raises(gh_app_runtime.GhAppError) as caught:
        gh_app_api.mint_token(
            env_for(tmp_path, Store({}), github),
            app_id=APP_ID,
            pem=pem,
            repository=REPO,
            permissions=MINT_PERMISSIONS,
        )
    assert str(caught.value) == (
        "the app private key is not an RSA key"
        if kind == "ec"
        else "private_key does not parse"
    )
    assert pem not in str(caught.value)
    assert github.requests == []


@pytest.mark.parametrize("failure", ["missing", "reply", "transport"])
def test_mint_token_errors_never_disclose_tokens(
    tmp_path: Path,
    rsa: Key,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    before = list(tmp_path.rglob("*"))
    routes = {} if failure == "missing" else ok_routes()
    routes[("POST", "/app/installations/7/access_tokens")] = httpx2.Response(
        403,
        json={"token": TOKEN, "message": TOKEN},
    )
    github = Github(routes)

    def transport(request: httpx2.Request) -> httpx2.Response:
        if failure == "transport":
            raise httpx2.ReadError(TOKEN)
        return github(request)

    env = replace(
        env_for(tmp_path, Store({}), github),
        transport=httpx2.MockTransport(transport),
    )
    with pytest.raises(gh_app_runtime.GhAppError) as caught:
        gh_app_api.mint_token(
            env,
            app_id=APP_ID,
            pem=rsa.pem,
            repository=REPO,
            permissions=MINT_PERMISSIONS,
        )
    assert str(caught.value) == "GitHub App token mint failed"
    assert TOKEN not in str(caught.value)
    assert capsys.readouterr() == ("", "")
    assert list(tmp_path.rglob("*")) == before
    assert all(TOKEN not in p.read_text() for p in tmp_path.rglob("*") if p.is_file())
