---
description: The run state files for a bundled agent launch.
status: draft
tags: [alpha]
title: Agent launch
type: Design Draft
---

# Agent launch

`agent launch` isn't built yet. This run-folder contract describes what later pull requests add. For each run, the driver sends its build of `tbhbdev` to the host so the host can start the run from that build.

`NAME` is the run name. The following paths are relative to the run state folder.

- `NAME.cli.tar` holds the bundle. The host checks the `sha256` digest before it unpacks the bundle. After unpacking, it deletes the archive.
- `NAME.cli/lib/` holds the unpacked tree at the Python import root, including `requirements.txt`.
- `NAME.cli/venv/` holds the virtual environment that uv builds from the pinned requirements. A `.pth` file points at `NAME.cli/lib/` so the interpreter can import the bundle in isolated mode.
- `NAME.cli/bin/tbhbdev` and `NAME.cli/bin/gh` are two-line `sh` entry points. Each uses `exec` to start Python from `NAME.cli/venv/` in isolated mode.
- `NAME.build` holds JSON with `commit`, `sha256`, and `module`. `agent run` writes it before the dispatcher starts.

After writing the result file, the host removes `NAME.cli/`.
