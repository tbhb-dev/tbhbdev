"""Fixtures for the GitHub App command, signer and HTTP client tests."""

# cspell:words getproxies paramgen

import subprocess

import httpx2
import pytest
from integration.alpha.gh_app_fakes import Key

from tbhbdev.alpha.gh_app_runtime import RUN_ENV


@pytest.fixture(scope="module")
def rsa(tmp_path_factory: pytest.TempPathFactory) -> Key:
    directory = tmp_path_factory.mktemp("rsa")
    private = directory / "private.pem"
    public = directory / "public.pem"
    genpkey = ["openssl", "genpkey", "-algorithm", "RSA", "-out", str(private)]
    subprocess.run([*genpkey, "-pkeyopt", "rsa_keygen_bits:2048"], check=True)
    subprocess.run(
        ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
    )
    return Key(private, public)


@pytest.fixture(scope="module")
def ec_key(tmp_path_factory: pytest.TempPathFactory) -> Key:
    """A real EC key, which openssl parses and the App can't use."""
    directory = tmp_path_factory.mktemp("ec")
    private = directory / "private.pem"
    genpkey = ["openssl", "genpkey", "-algorithm", "EC", "-out", str(private)]
    subprocess.run([*genpkey, "-pkeyopt", "ec_paramgen_curve:P-256"], check=True)
    return Key(private, directory / "unused.pem")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail at once on any request that no MockTransport answers."""
    monkeypatch.delenv(RUN_ENV, raising=False)

    def refuse(*_args: object, **_kwargs: object) -> httpx2.Response:
        message = "a test reached the real network"
        raise AssertionError(message)

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)
    # A client with no transport of its own looks up the host's proxies first,
    # which crashes a forked mutmut worker on macOS before it can send anything.
    monkeypatch.setattr("httpx2._utils.getproxies", refuse)
