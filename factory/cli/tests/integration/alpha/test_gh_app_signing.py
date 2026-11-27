# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""GitHub App JWT signing and key file handling."""

import base64
import json
import re
import subprocess
from pathlib import Path

import pytest
from integration.alpha.gh_app_fakes import (
    APP_ID,
    APP_VAULT,
    BEGIN,
    END,
    NOW,
    REPO,
    Github,
    Key,
    Store,
    app_store,
    env_for,
    mode,
    out_path,
    run,
)

from tbhbdev.alpha import (
    gh_app_runtime,
    gh_app_signing,
)
from tbhbdev.alpha.agent import host_tools

pytestmark = pytest.mark.usefixtures("no_network")


def test_rebuild_pem_refolds_a_flattened_key() -> None:
    body = "A" * 64 + "B" * 64 + "C" * 3
    flat = f"{BEGIN} {body[:50]}  {body[50:]} \n{END}"
    assert gh_app_signing.rebuild_pem(flat) == (
        f"{BEGIN}\n{'A' * 64}\n{'B' * 64}\nCCC\n{END}\n"
    )


def test_rebuild_pem_keeps_a_proper_key_and_the_label(rsa: Key) -> None:
    assert gh_app_signing.rebuild_pem(rsa.pem) == rsa.pem
    flat = rsa.pem.replace("\n", " ")
    assert gh_app_signing.rebuild_pem(flat) == rsa.pem


def test_rebuild_pem_leaves_other_text_to_openssl() -> None:
    assert gh_app_signing.rebuild_pem("not a key") == "not a key"
    assert gh_app_signing.rebuild_pem("-----BEGIN A-----x-----END B-----") == (
        "-----BEGIN A-----x-----END B-----"
    )


def decode(part: str) -> object:
    assert "=" not in part
    assert re.fullmatch(r"[A-Za-z0-9_-]*", part)
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_signing_input() -> None:
    header, payload = gh_app_signing.signing_input("123", NOW).decode().split(".")
    assert decode(header) == {"alg": "RS256", "typ": "JWT"}
    assert decode(payload) == {"iat": NOW - 60, "exp": NOW + 540, "iss": "123"}
    raw = gh_app_signing.signing_input("123", NOW).decode().split(".")
    assert base64.urlsafe_b64decode(raw[0] + "==") == b'{"alg":"RS256","typ":"JWT"}'
    assert base64.urlsafe_b64decode(raw[1] + "==") == (
        f'{{"iat":{NOW - 60},"exp":{NOW + 540},"iss":"123"}}'.encode()
    )


def test_b64url_drops_padding_and_uses_the_url_alphabet() -> None:
    assert gh_app_signing.b64url(b"\xfb\xff") == "-_8"
    assert gh_app_signing.b64url(b"a") == "YQ"
    assert gh_app_signing.b64url(b"\x00\x00\x17") == "AAA" + "X"
    assert gh_app_signing.b64url(b"") == ""


RSA_TEXT = (
    b"Private-Key: (2048 bit, 2 primes)\nmodulus:\n    00:aa\npublicExponent: 65537\n"
)


@pytest.mark.parametrize(
    ("text", "rsa_key"),
    [
        (RSA_TEXT, True),
        (b"RSA " + RSA_TEXT, True),
        (b"Private-Key: (256 bit)\npriv:\n    01\nmodulus:\n", True),
        (b"Private-Key: (256 bit)\npriv:\n    01\npub:\n    04\n", False),
        (b"Private-Key: (2048 bit)\nP:\n    01\nGenerator: 2\n", False),
        (b"Public-Key: (2048 bit)\nmodulus:\n    00\n", False),
        (b"x\n" + RSA_TEXT, False),
        (b" " + RSA_TEXT, False),
        (b"Private-Key: (256 bit)\npriv:\n  modulus:\n", False),
        (b"Private-Key: (256 bit)\nthe modulus:\n", False),
        (b"Private-Key:(2048 bit)\nmodulus:\n", False),
        (b"", False),
    ],
)
def test_is_rsa(text: bytes, rsa_key: bool) -> None:
    assert gh_app_signing.is_rsa(text) is rsa_key


def verify(jwt: str, key: Key, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    head, payload, signature = jwt.split(".")
    data = tmp_path / "data"
    sig = tmp_path / "sig"
    data.write_text(f"{head}.{payload}")
    sig.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    return subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(key.public),
            "-signature",
            str(sig),
            str(data),
        ],
        capture_output=True,
        check=False,
    )


def test_the_jwt_is_a_real_rs256_signature(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    store = Store(
        {
            f"op://{APP_VAULT}/github-app-bot/app_id": f" {APP_ID}\n",
            f"op://{APP_VAULT}/github-app-bot/private_key": rsa.pem,
        }
    )
    run(capsys, env_for(tmp_path, store, github), "bot", REPO, "--vault", APP_VAULT)
    head, payload, _ = github.jwt.split(".")
    assert decode(head) == {"alg": "RS256", "typ": "JWT"}
    assert decode(payload) == {"iat": NOW - 60, "exp": NOW + 540, "iss": APP_ID}
    assert verify(github.jwt, rsa, tmp_path).returncode == 0
    head, payload, signature = github.jwt.split(".")
    assert verify(f"{head}x.{payload}.{signature}", rsa, tmp_path).returncode != 0


def test_a_flattened_key_is_rebuilt_before_signing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    flat = rsa.pem.replace("\n", " ")
    code, _, _ = run(capsys, env_for(tmp_path, app_store(flat), github), "bot", REPO)
    assert code == 0
    assert verify(github.jwt, rsa, tmp_path).returncode == 0


def test_the_key_never_reaches_argv_and_its_file_is_private_and_gone(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    calls: list[tuple[list[str], int | None]] = []
    keyfiles: list[Path] = []

    def spy(
        argv: list[str], data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        keyfile = Path(argv[argv.index("-in") + 1] if "-in" in argv else argv[-1])
        keyfiles.append(keyfile)
        assert mode(keyfile) == 0o600
        assert mode(keyfile.parent) == 0o700
        assert keyfile.read_text() == rsa.pem
        assert rsa.body[:30] not in " ".join(argv)
        assert rsa.body[:30].encode() not in (data or b"")
        calls.append((argv, None))
        return gh_app_runtime.run_openssl(argv, data)

    env = env_for(tmp_path, app_store(rsa.pem), Github(), openssl=spy)
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert [argv[:2] for argv, _ in calls] == [["pkey", "-in"], ["dgst", "-sha256"]]
    assert calls[0][0][3:] == ["-noout", "-text"]
    assert calls[1][0][2] == "-sign"
    assert len({k.parent for k in keyfiles}) == 1
    assert not keyfiles[0].parent.exists()


def test_the_key_file_is_gone_after_a_parse_failure(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    seen: list[Path] = []

    def spy(
        argv: list[str], data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        seen.append(Path(argv[2]))
        return gh_app_runtime.run_openssl(argv, data)

    github = Github()
    env = env_for(tmp_path, app_store("not a key"), github, openssl=spy)
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out, err) == (1, "", "private_key does not parse\n")
    assert github.requests == []
    assert not seen[0].parent.exists()
    assert not out_path(tmp_path).exists()


@pytest.mark.parametrize(("returncode", "stdout"), [(1, b"sig"), (0, b"")])
def test_a_failed_signature_is_an_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    returncode: int,
    stdout: bytes,
) -> None:
    def fake(
        argv: list[str], _data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "pkey":
            return subprocess.CompletedProcess(argv, 0, RSA_TEXT, b"")
        return subprocess.CompletedProcess(argv, returncode, stdout, b"")

    github = Github()
    env = env_for(tmp_path, app_store(rsa.pem), github, openssl=fake)
    assert run(capsys, env, "bot", REPO) == (1, "", "cannot sign the JWT\n")
    assert github.requests == []


def test_openssl_missing_is_an_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa: Key,
) -> None:
    monkeypatch.setattr(host_tools, "search", lambda: str(tmp_path))
    env = env_for(tmp_path, app_store(rsa.pem), Github())
    assert run(capsys, env, "bot", REPO) == (1, "", "openssl isn't installed\n")


def test_an_ec_key_is_refused_before_any_signature_or_request(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ec_key: Key
) -> None:
    assert ec_key.pem.startswith("-----BEGIN " + "PRIVATE KEY-----")
    calls: list[str] = []

    def spy(
        argv: list[str], data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv[0])
        return gh_app_runtime.run_openssl(argv, data)

    github = Github()
    env = env_for(tmp_path, app_store(ec_key.pem), github, openssl=spy)
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out, err) == (1, "", "the app private key is not an RSA key\n")
    assert calls == ["pkey"]
    assert github.requests == []
    assert ec_key.body[:30] not in err
    assert not out_path(tmp_path).exists()
    dry = run(capsys, env, "bot", REPO, "--dry-run")
    assert dry == (1, "", "the app private key is not an RSA key\n")
