"""Find GitHub App installations and exchange JWTs for scoped tokens."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import httpx2

from tbhbdev import github_tls
from tbhbdev.alpha.gh_app_args import split_repo
from tbhbdev.alpha.gh_app_runtime import GhAppError
from tbhbdev.alpha.gh_app_signing import sign_in_memory

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from tbhbdev.alpha.gh_app_runtime import Env

type Json = dict[str, object]
# A run this long of token characters in a failure message may be a credential.
SECRET_LIKE = re.compile(r"[A-Za-z0-9_.~+/=-]{20,}")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.~+/=-]+")
API = "https://api.github.com"
TIMEOUT = httpx2.Timeout(10.0, connect=5.0)
MINT_FAILURE = "GitHub App token mint failed"


class InstallationMissingError(GhAppError):
    """Neither the repository nor its owner has an installation."""


class CachedInstallationMissingError(GhAppError):
    """A supplied installation id returned 404."""


@dataclass(frozen=True)
class Minted:
    token: str = field(repr=False)
    expires_at: int


def lookup_paths(repo: str) -> list[str]:
    if "/" not in repo:
        return [f"/orgs/{repo}/installation"]
    owner, _ = split_repo(repo)
    return [f"/repos/{repo}/installation", f"/orgs/{owner}/installation"]


def token_body(
    repo: str,
    *,
    all_repos: bool,
    permissions: Mapping[str, str] | tuple[tuple[str, str], ...] = (),
) -> Json:
    body: Json = {} if all_repos else {"repositories": [split_repo(repo)[1]]}
    if permissions:
        body["permissions"] = dict(permissions)
    return body


def json_object(response: httpx2.Response) -> Json:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def detail(payload: Json) -> str:
    """GitHub's own `message` field, minus anything that looks like a credential."""
    reason = payload.get("message")
    if not isinstance(reason, str) or not reason:
        return "no message"
    return SECRET_LIKE.sub("[redacted]", reason)


def installation_id(response: httpx2.Response) -> int | None:
    found = json_object(response).get("id")
    if response.status_code != httpx2.codes.OK or not isinstance(found, int):
        return None
    return found


def expiry(value: object) -> int | None:
    """The epoch an ISO 8601 VALUE with a zone names, or None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if moment.tzinfo is None else int(moment.timestamp())


def parse_token(payload: Json) -> Minted:
    """The token and its expiry in GitHub's reply.

    Raises:
        GhAppError: The reply lacks either. The message quotes GitHub's own
            `message` field and nothing else.
    """
    token = payload.get("token")
    epoch = expiry(payload.get("expires_at"))
    if isinstance(token, str) and TOKEN_PATTERN.fullmatch(token) and epoch is not None:
        return Minted(token, epoch)
    message = f"GitHub did not return a token: {detail(payload)}"
    raise GhAppError(message)


def find_installation(client: httpx2.Client, repo: str) -> int | None:
    """The installation of the repository, or else of its owner.

    Only a 404 moves on to the owner.

    Raises:
        GhAppError: GitHub answered a lookup with anything else but an id.
    """
    for path in lookup_paths(repo):
        response = client.get(path)
        if response.status_code == httpx2.codes.NOT_FOUND:
            continue
        ident = installation_id(response)
        if ident is None:
            message = (
                f"the installation lookup failed with status {response.status_code}: "
                f"{detail(json_object(response))}"
            )
            raise GhAppError(message)
        return ident
    return None


def exchange_token(  # noqa: PLR0913  # Optional installation cache seams.
    env: Env,
    repository: str,
    jwt: str,
    body: Json,
    *,
    known_installation: int | None = None,
    installation_found: Callable[[int], None] | None = None,
) -> Minted:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {jwt}",
    }
    try:
        with httpx2.Client(
            base_url=API,
            headers=headers,
            timeout=TIMEOUT,
            transport=env.transport,
            trust_env=False,
            verify=github_tls.public_roots(),
        ) as client:
            ident = known_installation
            if ident is None:
                ident = find_installation(client, repository)
            if ident is None:
                raise InstallationMissingError
            if installation_found is not None:
                installation_found(ident)
            reply = client.post(
                f"/app/installations/{ident}/access_tokens",
                json=body,
            )
    except httpx2.HTTPError as exc:
        message = f"the GitHub request failed: {type(exc).__name__}"
        raise GhAppError(message) from None
    if known_installation is not None and reply.status_code == httpx2.codes.NOT_FOUND:
        raise CachedInstallationMissingError
    return parse_token(json_object(reply))


def mint_token(  # noqa: PLR0913  # Explicit credentials and installation cache.
    env: Env,
    *,
    app_id: str,
    pem: str,
    repository: str,
    permissions: Mapping[str, str],
    known_installation: int | None = None,
    installation_found: Callable[[int], None] | None = None,
) -> Minted:
    """Mint a token with explicit scope, keeping the key and token in memory.

    REPOSITORY names OWNER/REPO, or just OWNER for organization permissions.

    Raises:
        GhAppError: Permissions, signing or the token exchange failed.
    """
    if not permissions or any(
        value not in {"read", "write"} for value in permissions.values()
    ):
        message = "permissions must be nonempty and contain only read or write"
        raise GhAppError(message)
    jwt = sign_in_memory(env, app_id, pem)
    body = token_body(
        repository, all_repos="/" not in repository, permissions=permissions
    )
    try:
        return exchange_token(
            env,
            repository,
            jwt,
            body,
            known_installation=known_installation,
            installation_found=installation_found,
        )
    except InstallationMissingError:
        raise InstallationMissingError(MINT_FAILURE) from None
    except CachedInstallationMissingError:
        raise
    except GhAppError:
        # An API error may echo a credential in its message field.
        raise GhAppError(MINT_FAILURE) from None
