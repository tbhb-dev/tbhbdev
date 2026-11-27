---
type: Readme
title: The agent harness role
description: Harness the agent account and keep agent signing away from the SSH agent.
tags: [ansible, agents, macos, ubuntu]
status: draft
generated:
  - role: generated
    harness: claude-code:2.1.322
    model: anthropic:claude-sonnet-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    subagent: sha-256-64:68226c066fc8a2a0
    at: "2026-11-11T22:41:57Z"
---

<!-- cspell:words chezmoi prek -->

On Ubuntu and macOS, `agent_harness` configures every account in `agent_harness_accounts`, which defaults to `agent`, the account other agent hosts log in to with their own per-host keys. Each account entry has a `name` and can set `home`, `repository`, and an optional `workspace` prerequisite. The home defaults to `/Users/<name>` on macOS and `/home/<name>` elsewhere. Each account's tasks go through `become_user` for that account, and nothing runs as root in a home, so nothing under a home ends up root-owned. `agent` has no `sudo`, and the connecting user becomes it. Package installs and the `chezmoi` binary stay outside the role, which uses `chezmoi` to parse TOML and has no other host-level work. On Linux, `agent_account` installs a pinned `chezmoi` release at `/usr/local/bin/chezmoi`, so run `agent-accounts.yml` or `lab-hosts.yml` first. The macOS hosts supply their own. Each account needs an existing signing key, `ssh-sign`, and repository clone, or the role fails closed, names what's missing, and never creates keys. The repository supplies the working directory for `tbhbdev alpha install` and its Git hooks. It defaults to `~/Code/github.com/tbhb-dev/tbhbdev`. Both lab inventory groups select the public `tbhb-dev/tools` clone instead, matching the workspace public clones when the lab groups turn private clones off. The lab harness also requires the `~/Code` workspace root. If the host uses a different workspace path, set `repository` on the account. See `meta/argument_specs.yml` for options.

Every account gets `tbhbdev alpha install`, then a merge of the agent environment into Claude Code `env` and Codex `shell_environment_policy.set`. `SSH_AUTH_SOCK` is empty. Git signs with that account's own agent public key in `.config/devtools/agent-signing-key.pub`, its own `.local/bin/ssh-sign`, and the agent author and committer identity. The role also turns off background updates in the Claude Code settings. Codex keeps its existing environment. The merge preserves unrelated settings and environment entries. JSON and TOML formatting and comments can change on the first merge. A later run with matching values leaves the files alone. The merge keeps symlinks and existing Claude file permissions. Codex settings are account-owned with mode `0600`, including existing files whose permissions need correction. New settings files use mode `0600`. TOML conversion uses `chezmoi`, including on Python 3.9 Macs.

Chose explicit repository and workspace paths in lab inventory over deriving the role default from workspace settings because the lab groups already turn private clones off there. Other groups keep their repository selection and prerequisites.

An account that already has `prek`, or `mise` to supply it, keeps working unchanged and doesn't need a pin. That covers the macOS accounts, where the installer gets `prek` through `mise`. An account with neither, such as a lab account that has `uv`, gets `uv tool install prek==<version>` into `~/.local/bin` before the installer runs. Both tool searches use the installer's `PATH`, which includes `~/.local/bin`, the mise shims and `/opt/homebrew/bin`. The version comes from the `[[tools.prek]]` entry in the repository clone's `mise.lock` on the target. That clone's hooks are what `prek` runs, so its lock is the source, and the ops root `mise.lock` omits `prek`. Missing `prek`, `mise` and pin fail the role. The role leaves a present `prek` at another version than the pin unchanged. `uv` must already sit in the account's `~/.local/bin` when the install runs.

Check mode uses the installer's `--dry-run` and the playbook preview for file operations. It still checks prerequisites. When `prek` and mise are both missing, check mode reports the pending install as a change and skips the dry run, because the installer can't run without `prek` and the install itself can't run in check mode. The coordinator runs `playbooks/agent-harness.yml --check --diff` on each target before applying. The controller must supply a key file and pin host keys in `ansible/known_hosts`.

The Ubuntu Molecule scenario creates the `agent` account, uses real `chezmoi` as the TOML parser, and an installer stand-in that, like the real one, fails without `prek` on its `PATH`, and a `uv` stand-in that installs a `prek` stand-in at the pinned version. The image has no `prek` or `mise`. It loads the Linux lab account settings and supplies the public tools clone alone. It checks the `prek` install, unchanged state on a repeat run, that an existing `prek` or mise works without a pin, the refusal when `prek`, mise and the pin are all missing, the installer working directory, missing workspace and clone refusals, the unchanged default repository, settings preservation, permissions repair and first-run creation, no root-owned files in the home, inline and nested TOML tables, repeated runs, and check mode. It can't verify the macOS keychain or the harness version. `tbhbdev alpha install` supplies the poll guard and the GitHub-call shim.
