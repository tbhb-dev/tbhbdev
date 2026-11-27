---
type: Readme
title: The Codex install role
description: Install a hash-pinned Codex release beside any other version for the agent account.
tags: [ansible, agents, codex]
status: draft
generated:
  - role: generated
    harness: claude-code:2.1.319
    model: anthropic:claude-opus-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    at: "2026-11-10T14:41:54Z"
---

`codex_cli` installs a pinned Codex release for the agent account. `playbooks/codex-cli.yml` installs and activates it on `agent_hosts:!windows`. Preview with `--check --diff`, then apply with `--limit` one node at a time. `agent-accounts.yml`, `lab-hosts.yml` and `lab-macos.yml` also run it after `agent_account`. `meta/argument_specs.yml` lists the options.

## The Codex install

Codex `codex_cli_version` goes to `codex_cli_account`, which defaults to `agent`. A check refuses `root` and `tony`. Tasks run as the account through `become_user`, so no file under its home ends up root-owned, and `~/.codex` is off limits. Reading the account's home from the host's account database, the role skips a host that lacks the account until the next run.

Each version unpacks into `~/.local/share/codex/<version>/`, with the executable `codex-<platform>` inside. The role downloads the release archive against the sha256 pin in `codex_cli_sha256`, then runs `tar -xzf`, which the macOS and Linux tars both accept, and sets the binary mode to `0755`. After it unpacks the archive, `<binary> --version` must end with the version. The role never deletes an old version.

Check mode reports pending directories and the active link without installing anything. When the run would create the version directory, it skips the download and unpack steps that depend on it. It also skips setting the binary mode until the binary exists, and skips the version check in check mode.

The active version is the symlink `~/.local/bin/codex`. By default the role leaves it alone. Setting `codex_cli_activate: true` points it at the pinned version.

## Switching versions and rolling back

1. Run the playbook with the new `codex_cli_version` and its hashes. The new version lands beside the old one.
2. Try `~/.local/share/codex/<new>/codex-<platform> --version` on the host.
3. Run the playbook again with `-e codex_cli_activate=true`.
4. To roll back, run `ln -sfn ~/.local/share/codex/<old>/codex-<platform> ~/.local/bin/codex` as the account, or run the playbook with the old version and `codex_cli_activate=true`.

## Hash provenance

`defaults/main.yml` defines the version and hashes. Update the hashes with the version. The pins are trust on first download: on 2026-11-10 in Coordinated Universal Time, @tbhb's agent fetched the three archives from `https://github.com/openai/codex/releases/download/rust-v0.166.0/` and hashed them. Each hash matches the digest the GitHub release API publishes for that asset.

## Installing for another account by hand

The role skips other accounts on purpose. To install for one yourself, read the values from `defaults/main.yml`, then run these steps as that account. Replace `<version>`, `<platform>` and `<sha256>` with the entry for the host's platform: `aarch64-apple-darwin` for Apple silicon macOS, `x86_64-unknown-linux-musl` or `aarch64-unknown-linux-musl` for Linux.

- **Asset URL:** `https://github.com/openai/codex/releases/download/rust-v<version>/codex-<platform>.tar.gz`
- **Install path:** `~/.local/share/codex/<version>/`, with the executable `codex-<platform>` inside after `tar -xzf`.
- **Check:** the archive's `sha256sum` equals `<sha256>` before unpacking, and `~/.local/share/codex/<version>/codex-<platform> --version` ends with `<version>`.
- **Symlink:** `ln -sfn ~/.local/share/codex/<version>/codex-<platform> ~/.local/bin/codex`, after `mkdir -p ~/.local/bin`. Skip it to install without switching.

## Verification scope

The `molecule/default/` scenario downloads the real pinned release into an Ubuntu container. It checks a first-install preview, convergence, unchanged state on repeat runs, the active command's version, binary permissions, and that nothing under the agent home is root-owned. Run it with `mise run molecule -- codex_cli` from the checkout root. Lint and syntax tests cover the rest.

The scenario runs on Linux and skips macOS, so no test covers the unpack with the macOS tar.
