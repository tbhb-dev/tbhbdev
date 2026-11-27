"""Read the host configuration and request before starting a run."""

from __future__ import annotations

import sys
import time
from pathlib import Path
lazy import argparse

from tbhbdev.alpha.agent import runfiles, settings
from tbhbdev.alpha.agent.ownership import RunExistsError
from tbhbdev.alpha.agent.run_host.request_plan import parse_request, select_host
from tbhbdev.alpha.agent.run_host.state import Run


def files_for(name: str) -> runfiles.RunFiles:
    home = Path.home()
    root = runfiles.state_home(home) / "tbhbdev" / "agent-runs"
    return runfiles.RunFiles(root, name)


def load(args: argparse.Namespace, tmpdir: Path) -> tuple[Run, str]:
    tmpdir = runfiles.private_dir(tmpdir / "tools")
    started = time.time()
    clock = time.monotonic()
    loaded = settings.load()
    host = select_host(loaded.hosts, args.host)
    home = Path.home()
    files = files_for(args.name)
    if files.path("request.json").exists() and args.request is not None:
        message = f"run {args.name!r} already exists"
        raise RunExistsError(message)
    raw = (
        sys.stdin.read()
        if args.request == "-"
        else Path(args.request or files.path("request.json")).read_text()
    )
    parsed = parse_request(raw, host, loaded.claude)
    text = Path(args.prompt or files.path("prompt")).read_text()
    if args.request is not None:
        files.receive(parsed)
    run = Run(
        files,
        parsed,
        loaded,
        host,
        home,
        tmpdir,
        started,
        clock + parsed.startup_deadline_seconds,
        steer=args.steer,
    )
    return run, text
