---
type: Readme
title: The agent host role
description: Prepare agent storage and the Linux sandbox runtime.
tags: [ansible, agents, ubuntu, macos]
status: draft
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:fc85a80fab0945c7
    at: "2026-11-11T18:51:05Z"
---

<!-- cspell:words bubblewrap bwrap -->

`agent_host` prepares storage before bootstrap creates the agent account. It installs the Linux `acl` tools and mounts separate volumes for the agent home and `/tmp`. Empty volume sizes leave those paths alone. macOS doesn't install packages and uses empty sizes with `/Users/example-user`.

## Linux sandbox

On Linux, the role installs `bubblewrap` for the `tbhbdev` Linux agent sandbox. It reads `kernel.apparmor_restrict_unprivileged_userns` and preserves its value, including Ubuntu's default of `1`. The allowance is specific to `/usr/bin/bwrap`.

The role prefers profiles named for `bwrap` that the `apparmor` package lists under `/etc/apparmor.d/`, including `bwrap-userns-restrict` and `usr.bin.bwrap`. Profiles for other programs stay outside this role. A missing package profile fails the run. If the package provides none, the role templates `/etc/apparmor.d/agent-host-bwrap` with `abi <abi/4.0>`, `flags=(unconfined)`, a `userns` allowance, and the optional `local/bwrap` include. It removes that fallback if a later package supplies a profile. A handler loads the selected profiles with `apparmor_parser -r` when the kernel restricts unprivileged user isolation. It runs before the sandbox probe.

Outside check mode, the `agent` account must pass `bwrap --ro-bind / / --dev /dev --proc /proc true` through `become_user: agent`. A fresh host has no account yet because this role precedes `agent_account`. In that case, a handler defers the probe until later in the play. Create the account before flushing handlers or ending the play.

## Options

The defaults and `meta/argument_specs.yml` describe the storage options. `agent_host_load_apparmor` and `agent_host_verify_bwrap` default to `true`. Limit overrides to test containers that can't load kernel policy or isolate a user.

## Verification

Run `mise run molecule -- agent_host` for the Ubuntu container scenario. It checks preview, convergence, unchanged state on repeat runs, installed packages, profile ownership and parsing, and preservation of the kernel restriction. It also parses the fallback template. The container shares its host's kernel. The scenario turns off policy loading and the unprivileged sandbox probe. A host run must verify those steps.
