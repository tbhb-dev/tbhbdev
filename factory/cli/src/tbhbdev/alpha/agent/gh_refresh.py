"""Refresh a build run's GitHub installation token from the host process."""

from __future__ import annotations

import os
import selectors
import shlex
import socket
import sys
import threading
import time
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import credentials
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from tbhbdev.alpha.agent.model import Credentials, RunRequest

SOCKET_NAME = "token.sock"
OK = "ok\n"
TIMEOUT = 30.0
CLIENT_SCRIPT = (
    f"import os,socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout({TIMEOUT});"
    f"s.connect(os.path.join(os.environ['GH_CONFIG_DIR'],{SOCKET_NAME!r}));"
    "r=s.makefile().readline();s.close();"
    f"sys.exit(0 if r=={OK!r} else (r.strip() or {credentials.FRESH_REFUSAL!r}))"
)


def credential_helper() -> str:
    """Refresh hosts.yml over the host socket before delegating to gh."""
    py = shlex.quote(str(Path(sys.base_prefix, "bin", "python3")))
    return (
        f'!f() {{ if [ "$1" = get ] && [ -S "${{GH_CONFIG_DIR:-}}/{SOCKET_NAME}" ];'
        f" then {py} -c {shlex.quote(CLIENT_SCRIPT)} || exit 1; fi;"
        ' exec gh auth git-credential "$@"; }; f'
    )


def _write_hosts_pinned(dir_fd: int, host: str, token: str) -> None:
    temp = f".hosts-{os.urandom(8).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(temp, flags, 0o600, dir_fd=dir_fd), "w") as out:
        out.write(credentials.hosts_yaml(host, token))
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, "hosts.yml", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)


def _unlink(dir_fd: int) -> None:
    with suppress(OSError):
        os.unlink(SOCKET_NAME, dir_fd=dir_fd)


def _loop(
    sel: selectors.BaseSelector,
    srv: socket.socket,
    wake: int,
    refresh: Callable[[], tuple[str, float]],
    delay: float,
) -> None:
    while True:
        if not (events := sel.select(timeout=delay)):
            _, delay = refresh()
            continue
        for key, _ in events:
            if key.fileobj == wake:
                return
            with suppress(OSError), srv.accept()[0] as client:
                client.settimeout(TIMEOUT)
                reply, delay = refresh()
                client.sendall(reply.encode())


@contextmanager
def serve(
    gh_dir: Path,
    request: RunRequest,
    refs: Credentials,
    initial: credentials.StoredToken,
    *,
    clock: Callable[[], float] = time.time,
) -> Generator[Path]:
    """Serve on-demand and proactive hosts.yml refreshes from the host."""
    host, stored, sock = repo.parts(request.repo)[0], initial, gh_dir / SOCKET_NAME
    ttl = credentials.FRESH_SECONDS
    with ExitStack() as cleanup:
        dir_fd = os.open(gh_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        wake_r, wake_w = os.pipe()
        for fd in (dir_fd, wake_w, wake_r):
            cleanup.callback(os.close, fd)
        srv = cleanup.enter_context(socket.socket(socket.AF_UNIX))
        sel = cleanup.enter_context(selectors.DefaultSelector())
        _unlink(dir_fd)
        srv.bind(str(sock))
        cleanup.callback(_unlink, dir_fd)
        sock.chmod(0o600)
        srv.listen(8)
        srv.settimeout(0.0)
        sel.register(srv, selectors.EVENT_READ)
        sel.register(wake_r, selectors.EVENT_READ)

        def refresh() -> tuple[str, float]:
            nonlocal stored
            try:
                tok = credentials.fresh_token(request, refs, stored, clock=clock)
                if tok is not None and tok != stored.token:
                    _write_hosts_pinned(dir_fd, host, tok)
                    stored = credentials.StoredToken(tok, clock())
            except Invalid, OSError:
                return f"{credentials.FRESH_REFUSAL}\n", 60.0
            else:
                return OK, max(stored.minted_at + ttl - clock(), 0.0)

        args = (sel, srv, wake_r, refresh, max(stored.minted_at + ttl - clock(), 0.0))
        thread = threading.Thread(target=_loop, args=args, daemon=True)
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(os.write, wake_w, b"x")
        yield sock


def refresh_if_served() -> None:
    """Ask the host token server to refresh hosts.yml when running inside a run."""
    gh_dir = os.environ.get("GH_CONFIG_DIR")
    if not gh_dir or not (sock := Path(gh_dir) / SOCKET_NAME).is_socket():
        return
    reply = ""
    with suppress(OSError), socket.socket(socket.AF_UNIX) as client:
        client.settimeout(TIMEOUT)
        client.connect(str(sock))
        reply = client.makefile().readline()
    if reply != OK:
        raise Invalid(reply.strip() or credentials.FRESH_REFUSAL)
