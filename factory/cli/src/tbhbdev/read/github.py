"""GET-only GitHub REST reads on the session's `gh` credential and budget."""

import os
import subprocess  # nosec B404
import time

import httpx2

from tbhbdev import github_tls, userconfig
from tbhbdev.alpha import gh_shim

API = "https://api.github.com"
HOST = "github.com"
TIMEOUT = 20
HTTP_OK = 200
HTTP_NOT_FOUND = 404
SETUP = (
    "the session has no GitHub credential for reads: `gh auth token` gave none. "
    "Run `tbhbdev alpha gh-app` with `--permissions` to get a read token, then "
    "make it the `gh` login of this session's GH_CONFIG_DIR."
)


class ToolError(Exception):
    """A tool, credential, network or budget failure; the message says which."""


class NotFoundError(Exception):
    """GitHub answered 404."""


def _token(gh: str) -> str:
    """The `gh` login's token, from a local read that makes no GitHub request."""
    try:
        done = subprocess.run(  # nosec B603 -- the resolved gh, fixed arguments
            [gh, "auth", "token", "--hostname", HOST],
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT,
        )
    except OSError, subprocess.SubprocessError:
        raise ToolError(SETUP) from None
    token = done.stdout.strip()
    if done.returncode != 0 or not token:
        raise ToolError(SETUP)
    return token


def _reserve(gh: str) -> None:
    """Count one call against the session's gh budget; none outside a session."""
    root = gh_shim.session_root(os.environ)
    if root is None:
        return
    try:
        gh_shim.reserve(gh, root, time.time(), *gh_shim.limits())
    except (gh_shim.RefusalError, userconfig.ConfigError, OSError) as exc:
        message = f"refused by the agent gh budget: {exc}"
        raise ToolError(message) from None


def _only_bare_get(request: httpx2.Request) -> None:
    try:
        has_body = bool(request.content)
    except httpx2.RequestNotRead:
        has_body = True
    if request.method != "GET" or has_body:
        message = "refused: reads send only GET requests without a body"
        raise ToolError(message)


def build_client(
    token: str, *, transport: httpx2.BaseTransport | None = None
) -> httpx2.Client:
    """A client that refuses, before sending, any request but a bodiless GET."""
    return httpx2.Client(
        base_url=API,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        },
        timeout=TIMEOUT,
        follow_redirects=False,
        trust_env=False,
        verify=github_tls.public_roots(),
        transport=transport,
        event_hooks={"request": [_only_bare_get]},
    )


def get(path: str) -> object:
    """The JSON that GET PATH answers, after reserving one call of the budget.

    Raises:
        NotFoundError: GitHub answered 404.
        ToolError: There is no token, the budget refuses, the network fails,
            GitHub answers any other status than 200, or the body isn't JSON.
    """
    gh = gh_shim.real_gh(os.environ)
    if gh is None:
        raise ToolError(SETUP)
    token = _token(gh)
    _reserve(gh)
    try:
        with build_client(token) as client:
            response = client.get(path)
    except httpx2.HTTPError as exc:
        message = f"network error ({type(exc).__name__})"
        raise ToolError(message) from None
    status = response.status_code
    if status == HTTP_NOT_FOUND:
        raise NotFoundError
    if status != HTTP_OK:
        message = f"GitHub answered HTTP {status}"
        raise ToolError(message)
    try:
        return response.json()
    except ValueError:
        message = "malformed response: the body isn't JSON"
        raise ToolError(message) from None
