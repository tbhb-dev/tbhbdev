"""Steer the active turn on a run's private Codex app server."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shlex
import sys
from contextlib import suppress
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions import runfiles_names
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

type Rpc = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]

CLAUDE_UNAVAILABLE = "Claude delivery is not built"
UNLOADED = "Codex thread is unknown or unloaded"
SOCKET = "steer.sock"
TEXT_FRAME = 0x81
EXTENDED_LENGTH = 126
LARGE_LENGTH = 127


def launch_command(command: tuple[str, ...], codex_home: Path) -> tuple[str, ...]:
    """Keep the server and exec client in the supervisor's owned process group."""
    socket = codex_home / SOCKET
    endpoint = f"unix://{socket}"
    output = command.index("--output-last-message")
    start = command.index("-C") + 2
    server = [command[0], "app-server", "--listen", endpoint, "--strict-config"]
    keys = {"-s": "sandbox_mode", "-m": "model"}
    for option, value in batched(command[start:output], 2, strict=True):
        setting = value if option == "-c" else f"{keys[option]}={json.dumps(value)}"
        server.extend(("-c", setting))
    client = (command[0], "exec", "--remote", endpoint, "--json", *command[output:])
    path = shlex.quote(str(socket))
    script = f"""\
{shlex.join(server)} </dev/null >&2 &
server=$!
trap 'kill "$server" 2>/dev/null; wait "$server" 2>/dev/null' EXIT
trap 'exit 143' HUP INT TERM
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    test -S {path} && break
    kill -0 "$server" || exit 1
    sleep 1
done
test -S {path} || exit 1
{shlex.join(client)}
"""
    return ("/bin/sh", "-c", script)


async def steer(rpc: Rpc, thread: str, message: str) -> None:
    result = await rpc(
        "thread/turns/list", {"threadId": thread, "limit": 1, "itemsView": "notLoaded"}
    )
    turns = cast("list[dict[str, str]]", result["data"])
    if not turns or turns[0]["status"] != "inProgress":
        detail = "Codex thread has no active turn"
        raise Invalid(detail)
    await rpc(
        "turn/steer",
        {
            "threadId": thread,
            "expectedTurnId": turns[0]["id"],
            "input": [{"type": "text", "text": message, "text_elements": []}],
        },
    )


async def proxy(command: tuple[str, ...], thread: str, message: str) -> None:
    env = None
    if sys.platform == "linux":
        path = os.environ.get("PATH", os.defpath)
        env = {**os.environ, "PATH": f"{path}:/usr/local/bin"}
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    writer = cast("asyncio.StreamWriter", process.stdin)
    reader = cast("asyncio.StreamReader", process.stdout)
    request_id = 0

    async def write(payload: dict[str, object]) -> None:
        writer.write(frame(json.dumps(payload).encode()))
        await writer.drain()

    async def rpc(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal request_id
        request_id += 1
        await write({"id": request_id, "method": method, "params": params})
        while True:
            response: dict[str, object] = json.loads(await read_frame(reader))
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = cast("dict[str, str]", response["error"])
                raise Invalid(error["message"])
            return cast("dict[str, object]", response["result"])

    try:
        async with asyncio.timeout(10):
            key = base64.b64encode(secrets.token_bytes(16))
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                b"Sec-WebSocket-Key: " + key + b"\r\n\r\n"
            )
            await writer.drain()
            if not (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101 "):
                raise Invalid(UNLOADED)
            await rpc("initialize", {"clientInfo": {"name": "tbhbdev", "version": "0"}})
            await write({"method": "initialized"})
            await steer(rpc, thread, message)
    finally:
        with suppress(ProcessLookupError):
            process.kill()
        await process.communicate()


def frame(payload: bytes) -> bytes:
    """Encode one masked text message for the proxy's WebSocket connection."""
    length = len(payload)
    if length < EXTENDED_LENGTH:
        header = bytes((TEXT_FRAME, 0x80 | length))
    elif length < 1 << 16:
        header = bytes((TEXT_FRAME, 0x80 | EXTENDED_LENGTH)) + length.to_bytes(2, "big")
    else:
        header = bytes((TEXT_FRAME, 0x80 | LARGE_LENGTH)) + length.to_bytes(8, "big")
    mask = secrets.token_bytes(4)
    return header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    kind, length = await reader.readexactly(2)
    if kind != TEXT_FRAME:
        raise Invalid(UNLOADED)
    if length in {EXTENDED_LENGTH, LARGE_LENGTH}:
        length = int.from_bytes(
            await reader.readexactly(2 if length == EXTENDED_LENGTH else 8), "big"
        )
    return await reader.readexactly(length)


def thread_id(files: runfiles.RunFiles) -> str:
    with files.path("out").open() as events:
        event: dict[str, str] = json.loads(events.readline())
    if event.get("type") != "thread.started":
        raise Invalid(UNLOADED)
    return event["thread_id"]


def main(arguments: list[str], _prog: str) -> int:
    (name,) = arguments
    state = runfiles.state_home(Path.home())
    try:
        files = runfiles.RunFiles(state / runfiles_names.DIRECTORY, name)
        socket = state / "tbhbdev" / "agent-codex" / files.name / SOCKET
        thread = thread_id(files)
        message = sys.stdin.read()
        command = ("codex", "app-server", "proxy", "--sock", str(socket))
        asyncio.run(proxy(command, thread, message))
    except Invalid as exc:
        print(exc)
    except TimeoutError:
        print("Codex app-server timed out")
    except OSError, asyncio.IncompleteReadError, json.JSONDecodeError:
        print(UNLOADED)
    return 0
