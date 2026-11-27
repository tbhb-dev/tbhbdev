---
type: Readme
title: The lab agent role
description: Configure the Linux lab agent's runtime and shell environment.
tags: [ansible, agents, ubuntu]
status: draft
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: medium
    surface: exec
    session: sha-256-64:9f1742a500f12a3c
    at: "2026-11-13T15:29:49Z"
---

`lab_agent` configures rootless Docker under `gVisor` and the `agent` account on host-a, host-b, and host-c. host-d falls outside this Linux role. On the hosts, `agy` moved from 1.5.7 to 1.5.8 without a role change. Managed `/home/example-user/.agy-profile` exports `AGY_CLI_DISABLE_AUTO_UPDATE=true` to turn off [background self-updates](https://antigravity.google/docs/cli/troubleshooting). Keeping the variable set prevents automatic upgrades but doesn't pin or install 1.5.8. Marker blocks at the start of `.profile` and `.bashrc` source the drop-in for login and interactive Bash shells while preserving the hand-made PATH entries. A profile export doesn't reach `ssh host sh -c`. For remote jobs, the agy-job runner sets it explicitly in its own remote command. Undo: remove the drop-in. Guarded source blocks tolerate its absence.
