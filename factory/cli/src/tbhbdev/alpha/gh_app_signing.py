"""Normalize PEM keys and sign GitHub App JWTs."""

import base64
import json
import re
import tempfile
from typing import TYPE_CHECKING

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tbhbdev.alpha.gh_app_runtime import GhAppError, fill

if TYPE_CHECKING:
    from tbhbdev.alpha.gh_app_runtime import Env

PEM_PATTERN = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL
)
JWT_BACKDATE = 60
JWT_LIFETIME = 540
PEM_WIDTH = 64
# The text of `openssl pkey -noout -text` for an RSA key. Older OpenSSL builds
# put "RSA " before the header.
RSA_HEADER = re.compile(rb"\A(?:RSA )?Private-Key: \(")
RSA_MODULUS = re.compile(rb"^modulus:", re.MULTILINE)
HEADER = {"alg": "RS256", "typ": "JWT"}


def rebuild_pem(raw: str) -> str:
    """A PEM with proper lines from RAW, which a concealed field may flatten.

    Text that holds no BEGIN and END pair comes back unchanged, for openssl to
    refuse.
    """
    found = PEM_PATTERN.search(raw)
    if found is None:
        return raw
    label, body = found.groups()
    body = "".join(body.split())
    lines = [body[i : i + PEM_WIDTH] for i in range(0, len(body), PEM_WIDTH)]
    return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----\n"])


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims(app_id: str, now: int) -> dict[str, object]:
    return {"iat": now - JWT_BACKDATE, "exp": now + JWT_LIFETIME, "iss": app_id}


def signing_input(app_id: str, now: int) -> bytes:
    parts = [
        b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in (HEADER, claims(app_id, now))
    ]
    return ".".join(parts).encode()


def is_rsa(text: bytes) -> bool:
    """Whether TEXT, from `openssl pkey -noout -text`, describes an RSA key."""
    return RSA_HEADER.match(text) is not None and RSA_MODULUS.search(text) is not None


def sign(env: Env, app_id: str, key: str) -> str:
    """A signed JWT for APP_ID, from the PEM KEY, which only a 0600 file holds."""
    with tempfile.TemporaryDirectory() as directory:
        # mkstemp creates the file 0600, before anything is written to it.
        handle, keyfile = tempfile.mkstemp(dir=directory)
        fill(handle, key)
        described = env.openssl(["pkey", "-in", keyfile, "-noout", "-text"])
        if described.returncode:
            message = "private_key does not parse"
            raise GhAppError(message)
        if not is_rsa(described.stdout):
            message = "the app private key is not an RSA key"
            raise GhAppError(message)
        data = signing_input(app_id, env.now())
        done = env.openssl(["dgst", "-sha256", "-sign", keyfile], data)
    if done.returncode or not done.stdout:
        message = "cannot sign the JWT"
        raise GhAppError(message)
    return f"{data.decode()}.{b64url(done.stdout)}"


def sign_in_memory(env: Env, app_id: str, pem: str) -> str:
    """Sign an App JWT without writing the private key to a file."""
    try:
        key = serialization.load_pem_private_key(
            rebuild_pem(pem).encode(), password=None
        )
    except ValueError, TypeError, UnsupportedAlgorithm:
        message = "private_key does not parse"
        raise GhAppError(message) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        message = "the app private key is not an RSA key"
        raise GhAppError(message)
    data = signing_input(app_id, env.now())
    signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return f"{data.decode()}.{b64url(signature)}"
