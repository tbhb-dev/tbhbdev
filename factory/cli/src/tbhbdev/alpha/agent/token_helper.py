"""Refresh GitHub App installation tokens outside the run sandbox."""

from __future__ import annotations

import os
import socket
import sys
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from tbhbdev.alpha import gh_app_runtime
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from tbhbdev.alpha.gh_app_api import Minted

REFRESH_MARGIN, RETRY_INTERVAL, _TIMEOUT = 600, 30.0, 10.0
GH_SOCKET = "gh.sock"
TOKEN_ERRORS = (
    Invalid,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    gh_app_runtime.GhAppError,
)


class TokenHelper:
    """Cache one installation token and re-mint inside ten minutes or on 401."""

    def __init__(
        self,
        mint: Callable[[], Minted],
        initial: Minted,
        *,
        clock: Callable[[], int] | None = None,
        on_refresh: Callable[[str], None] | None = None,
    ) -> None:
        self._mint, self._current = mint, initial
        self._clock, self._on_refresh = clock or gh_app_runtime.clock, on_refresh
        self._lock, self.rejected = threading.Lock(), False

    def __repr__(self) -> str:
        return "<TokenHelper>"

    def _mint_locked(self) -> str:
        self._current = self._mint()
        if self._on_refresh:
            self._on_refresh(self._current.token)
        return self._current.token

    def token(self, *, force: bool = False) -> str:
        """Return cached token when >10 minutes remain, else mint a new one."""
        with self._lock:
            if not force and self._current.expires_at - self._clock() > REFRESH_MARGIN:
                return self._current.token
            return self._mint_locked()

    def retry(self) -> str:
        """Record a 401 rejection and mint a fresh token."""
        with self._lock:
            self.rejected = True
            return self._mint_locked()

    def _handle(self, conn: socket.socket) -> None:
        with conn, suppress(*TOKEN_ERRORS):
            conn.settimeout(_TIMEOUT)
            cmd = conn.recv(64).decode(errors="ignore").strip()
            if cmd == "store":
                self.rejected, reply = False, "ok"
            elif cmd in {"get", "erase"}:
                reply = self.token() if cmd == "get" else self.retry()
            else:
                return
            conn.sendall(f"{reply}\n".encode())

    def step(self, sock: socket.socket, stop: threading.Event) -> float | None:
        try:
            conn, _ = sock.accept()
        except TimeoutError:
            with suppress(*TOKEN_ERRORS):
                self.token()
                return None
            return RETRY_INTERVAL
        except OSError:
            stop.set()
            return None
        conn.close() if stop.is_set() else self._handle(conn)
        return None

    @contextmanager
    def serve(self, path: Path, *, min_wait: float = 1.0) -> Generator[None]:
        """Serve fresh tokens over a private Unix socket and refresh hosts.yml."""
        stop, sock = threading.Event(), socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        path.chmod(0o600)
        sock.listen(8)

        def loop() -> None:
            retry_wait: float | None = None
            while not stop.is_set():
                with self._lock:
                    rem = self._current.expires_at - REFRESH_MARGIN - self._clock()
                sock.settimeout(retry_wait or max(min_wait, float(rem)))
                retry_wait = self.step(sock, stop)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            with suppress(OSError), socket.socket(socket.AF_UNIX) as wake:
                wake.connect(str(path))
            thread.join(timeout=5.0)
            sock.close()
            path.unlink(missing_ok=True)


def send(gh_dir: str | None, command: str) -> str | None:
    """Send COMMAND to run-host's token socket if present, returning its reply."""
    path = Path(gh_dir).parent / GH_SOCKET if gh_dir else None
    if path and path.is_socket():
        with suppress(OSError), socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(_TIMEOUT)
            conn.connect(str(path))
            conn.sendall(f"{command}\n".encode())
            conn.shutdown(socket.SHUT_WR)
            return conn.recv(4096).decode(errors="ignore").strip() or None
    return None


def _hosts_token(gh_dir: str | None) -> str | None:
    with suppress(OSError, yaml.YAMLError, TypeError):
        data = yaml.safe_load((Path(str(gh_dir)) / "hosts.yml").read_text())
        tok = (data.get(repo.DEFAULT_GIT_HOST) or {}).get("oauth_token")
        return tok if isinstance(tok, str) and tok else None
    return None


def git_credential_main(argv: list[str], _prog: str) -> int:
    """Git credential helper backed by run-host's token socket or hosts.yml."""
    action = argv[0] if argv else ""
    fields = dict(ln.split("=", 1) for ln in sys.stdin.read().splitlines() if "=" in ln)
    if fields.get("protocol") != "https" or fields.get("host") != repo.DEFAULT_GIT_HOST:
        return 0
    gh_dir = os.environ.get("GH_CONFIG_DIR")
    if action == "get" and (tok := send(gh_dir, "get") or _hosts_token(gh_dir)):
        sys.stdout.write(
            f"protocol=https\nhost={repo.DEFAULT_GIT_HOST}\n"
            f"username=x-access-token\npassword={tok}\n"
        )
    elif action in {"erase", "store"}:
        send(gh_dir, action)
    return 0
