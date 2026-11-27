---
description: Configuration from installed packages.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    session: sha-256-64:b92e3fe7d3cd5f25
    at: "2026-11-27T15:42:02Z"
status: stable
tags: [plugins]
title: Plugins
type: Reference
---

# Plugins

An installed package can supply config through an entry point in the `tbhbdev.plugins` group. The entry point resolves to an object whose `config(name: str) -> str | None` returns the contents of the file identified by `name`, or `None`.

Plugins load on first use. When one fails to import, `tbhbdev` names it and the error on stderr, and the built-in commands keep working. To run `tbhbdev` with a plugin, install both in one environment:

```sh
uvx --from "git+https://github.com/tbhb-dev/tbhbdev#subdirectory=factory/cli" --with <plugin package> tbhbdev <tool> [args...]
```
