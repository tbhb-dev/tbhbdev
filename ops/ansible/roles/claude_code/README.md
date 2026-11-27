---
type: Readme
title: The Claude Code install role
description: Install a pinned native Claude Code release for an agent account.
tags: [ansible, agents, claude]
status: draft
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:b4d972db8e21bc38
    at: "2026-11-23T07:50:49Z"
---

`claude_code` installs Claude Code for `claude_code_account`, which defaults to `agent`. It runs after `codex_cli` in `lab-hosts.yml`, `lab-macos.yml`, and `agent-accounts.yml`. The role refuses `root` and `tony` and skips a missing account. Tasks that write into the home use `become_user: "{{ claude_code_account }}"`. `meta/argument_specs.yml` lists the options.

## Installation and updates

The role stores each version in `~/.local/share/claude/<version>/claude`. The role checks the download against its platform's sha256 pin before making the binary executable. It runs `--version` without logging in or making an authenticated request. It leaves credentials untouched.

By default, `~/.local/bin/claude` links directly to that version's verified native binary. Set `claude_code_activate: false` to install beside the active version without switching it. The `agent_harness` role sets the background update variable to `"1"` in the account's Claude Code settings `env`. Anthropic documents this [environment variable](https://code.claude.com/docs/en/setup#disable-auto-updates). It stops background updates, including when the account invokes the versioned binary directly. Run `agent-harness.yml` to apply the setting. Explicit `claude update` and `claude install` remain available.

The role leaves old versions in place. To upgrade, change `claude_code_version` and every platform hash together, then run the playbook. To roll back, use the previous version and its hashes. Check mode reports pending directories and the active link. It skips the version check and any download whose directory doesn't exist yet.

Each host needs a one-time interactive `/login` as agent before runs.

## Download and checksum provenance

Anthropic publishes the binaries through its [native release distribution](https://code.claude.com/docs/en/setup#binary-integrity-and-code-signing). The binary URL pattern is `https://downloads.claude.ai/claude-code-releases/<version>/<platform>/claude`. Fetch the published manifest at `https://downloads.claude.ai/claude-code-releases/<version>/manifest.json`. Adding `.sig` selects the detached signature. Supported platforms are `linux-x64` for Linux x86_64, `linux-arm64` for Linux aarch64, and `darwin-arm64` for Apple silicon macOS.

On 2026-11-23 in Coordinated Universal Time, @tbhb's agent selected 2.1.330 because the fleet already runs it. The Coordination decision on 2026-11-23 overrode the five-day rule for this pin.

For each platform, the agent copied `platforms[<platform>].checksum` from the version's manifest into `defaults/main.yml`. After downloading the three binaries, the agent computed their sha256 hashes. Every hash matched. Using `gpg --verify`, the agent also verified the manifest's detached signature with the Anthropic [release key](https://downloads.claude.ai/keys/claude-code.asc), checking its fingerprint against the documentation: `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`. Committed hashes govern installation, so upstream changes fail verification.

## Verification scope

Run `mise run molecule -- claude_code` from the checkout root. The scenario downloads the real pinned binary into an Ubuntu container and checks first-install preview, convergence, unchanged state on repeat runs, version, checksum, permissions, and home ownership. It also checks that the active symlink points directly to the pinned binary. The `agent_harness` scenario checks the update setting and preservation of unrelated settings. Linux arm64 and macOS need platform validation beyond this scenario.
