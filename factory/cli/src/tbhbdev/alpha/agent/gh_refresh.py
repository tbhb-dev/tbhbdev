"""Refresh a build run's GitHub installation token from the host process."""

from __future__ import annotations

import os
import secrets
import selectors
import shlex
import socket
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import credentials
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from tbhbdev.alpha.agent.model import Credentials, RunRequest

SOCKET_NAME = "token.sock"
OK = "ok\n"
REQUEST_TIMEOUT = 30.0
REFRESH_AFTER = credentials.TOKEN_LIFETIME_SECONDS - credentials.REFRESH_MARGIN_SECONDS
CLIENT_SCRIPT = (
    "import os,socket,sys;"
    f"s=socket.socket(socket.AF_UNIX);s.settimeout({REQUEST_TIMEOUT});"
    f"s.connect(os.path.join(os.environ['GH_CONFIG_DIR'],{SOCKET_NAME!r}));"
    "r=s.makefile().readline();s.close();"
    f"sys.exit(0 if r=={OK!r} else (r.strip() or {credentials.FRESH_REFUSAL!r}))"
)


def credential_helper() -> str:
    """Refresh hosts.yml over the host socket before delegating to gh."""
    python = shlex.quote(str(Path(sys.base_prefix, "bin", "python3")))
    return (
        "!f() { "
        f'if [ "$1" = get ] && [ -S "${{GH_CONFIG_DIR:-}}/{SOCKET_NAME}" ]; then '
        f"{python} -c {shlex.quote(CLIENT_SCRIPT)} || exit 1; "
        'fi; exec gh auth git-credential "$@"; }; f'
    )


def _write_hosts_pinned(dir_fd: int, host: str, token: str) -> None:
    temp = f".hosts-{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temp, flags, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(credentials.hosts_yaml(host, token))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, "hosts.yml", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)


class Refresher:
    """Serve on-demand and proactive hosts.yml refreshes from the host."""

    def __init__(
        self,
        request: RunRequest,
        references: Credentials,
        initial: credentials.StoredToken,
        *,
        clock: Callable[[], float] = time.time,
        source: Callable[
            [Credentials, str, dict[str, str]], str
        ] = credentials.pull_requests_token,
    ) -> None:
        self._request = request
        self._references = references
        self._host = repo.parts(request.repo)[0]
        self._stored = initial
        self._clock = clock
        self._source = source

    def _refresh(self, dir_fd: int) -> str:
        try:
            token = credentials.fresh_token(
                self._request,
                self._references,
                self._stored,
                clock=self._clock,
                source=self._source,
            )
            if token is None:
                return f"{credentials.FRESH_REFUSAL}\n"
            if token != self._stored.token:
                _write_hosts_pinned(dir_fd, self._host, token)
                self._stored = credentials.StoredToken(token, self._clock())
        except Invalid as exc:
            return f"{exc}\n"
        except OSError:
            return f"{credentials.FRESH_REFUSAL}\n"
        else:
            return OK

    def _loop(
        self,
        selector: selectors.BaseSelector,
        server: socket.socket,
        wake_r: int,
        dir_fd: int,
    ) -> None:
        next_due = self._stored.minted_at + REFRESH_AFTER
        while True:
            events = selector.select(timeout=max(next_due - self._clock(), 0.0))
            if not events:
                next_due = (
                    self._stored.minted_at + REFRESH_AFTER
                    if self._refresh(dir_fd) == OK
                    else self._clock() + 60.0
                )
                continue
            for key, _ in events:
                if key.fileobj == wake_r:
                    return
                with suppress(OSError):
                    client, _ = server.accept()
                    with client:
                        client.settimeout(REQUEST_TIMEOUT)
                        client.sendall(self._refresh(dir_fd).encode())
                due = self._stored.minted_at + REFRESH_AFTER
                next_due = self._clock() + 60.0 if due <= self._clock() else due

    @contextmanager
    def serve(self, gh_dir: Path) -> Generator[Path]:
        dir_fd = os.open(gh_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        sock_path = gh_dir / SOCKET_NAME
        wake_r, wake_w = os.pipe()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        selector = selectors.DefaultSelector()
        thread: threading.Thread | None = None
        try:
            with suppress(FileNotFoundError):
                os.unlink(SOCKET_NAME, dir_fd=dir_fd)
            server.bind(str(sock_path))
            sock_path.chmod(0o600)
            server.listen(8)
            server.settimeout(0.0)
            selector.register(server, selectors.EVENT_READ)
            selector.register(wake_r, selectors.EVENT_READ)
            args = (selector, server, wake_r, dir_fd)
            thread = threading.Thread(
                target=self._loop, args=args, name="gh-token-refresh", daemon=True
            )
            thread.start()
            yield sock_path
        finally:
            with suppress(OSError):
                os.write(wake_w, b"x")
            if thread is not None:
                thread.join(timeout=5)
            selector.close()
            server.close()
            os.close(wake_r)
            os.close(wake_w)
            with suppress(OSError):
                os.unlink(SOCKET_NAME, dir_fd=dir_fd)
            os.close(dir_fd)


def refresh_if_served(env: Mapping[str, str] | None = None) -> None:
    """Ask the host token server to refresh hosts.yml when running inside a run."""
    gh_dir = (os.environ if env is None else env).get("GH_CONFIG_DIR")
    if not gh_dir or not (sock := Path(gh_dir) / SOCKET_NAME).is_socket():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(REQUEST_TIMEOUT)
            client.connect(str(sock))
            reply = client.makefile().readline()
    except OSError:
        reply = ""
    if reply != OK:
        raise Invalid(reply.strip() or credentials.FRESH_REFUSAL)
