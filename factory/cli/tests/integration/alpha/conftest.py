"""Fixtures for the GitHub App command, signer and HTTP client tests."""

# cspell:words paramgen

import subprocess

import pytest
from integration.alpha.gh_app_fakes import Key


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
