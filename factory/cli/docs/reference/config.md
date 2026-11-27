---
description: The user configuration file and deployment settings.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.2-sol
    at: "2026-11-26T23:36:21Z"
status: stable
tags: [config]
title: Config
type: Reference
---

# Config

The values of one deployment come from the user config at `$XDG_CONFIG_HOME/tbhbdev/config.toml`, where `XDG_CONFIG_HOME` defaults to `.config` in the home directory. The package has no defaults for them, and no environment variable or plugin overrides them. When the file or a required key is missing, the command fails, and its error gives the file's path and the `[table] key`. Optional settings include the `[loop]`, `[agent]` and `[gh_budget]` tables. Calling `alpha loop` requires its settings in `[loop]`. Commands read the file when they first need a value. `--help` works without the file.

```toml
[op]
account = "example.1password.com"

[connect]
endpoints = ["https://connect.example.com"]

[gh_budget]
hourly_limit = 300
core_floor = 1000
```

`[gh_budget]` sets the agent `gh` budget that [GitHub polling](alpha/github-polling.md) describes. Its values are whole numbers, and the ones shown are the defaults.

The operator's host tooling outside this repository installs the config on each agent host. See [Agent hosts](agent-hosts.md) for the host requirements.
