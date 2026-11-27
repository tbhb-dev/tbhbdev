---
description: Config and credentials for agent hosts.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.2-sol
    at: "2026-11-26T23:36:21Z"
status: stable
tags: [agent-hosts]
title: Agent hosts
type: Reference
---

# Agent hosts

The operator's host tooling outside this repository installs the [user config](config.md) for the agent account. Set `[connect] endpoints` in failover order.

`tbhbdev` reads the 1Password Connect token from the agent keychain on macOS and from a private token file on Linux. See [Secrets](secrets.md) for the token path and permissions. The operator's host tooling also installs the private gitleaks config.
