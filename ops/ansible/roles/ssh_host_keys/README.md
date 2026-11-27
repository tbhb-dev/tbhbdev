---
type: Readme
title: Keys for each host
description: Give the agent and ops accounts each their own ed25519 key on a host, and print the public halves.
tags: [ansible, agents, ssh]
status: draft
generated:
  - role: generated
    harness: claude-code:2.1.319
    model: anthropic:claude-sonnet-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    subagent: sha-256-64:3244b3cb4706f000
    at: "2026-11-09T18:31:15Z"
---

`ssh_host_keys` handles each account in `ssh_host_keys_users`, which defaults to `agent` and `ops`. Each gets its own ed25519 key at `~/.ssh/id_ed25519`, and the role prints the public half. It's step 1 of a move to per-host keys. `playbooks/ssh-keys.yml` runs it on `agent_hosts:!windows`, connecting as `ops` with become, like the inventory's other plays.

## Steps for each account

- **Account.** The role runs `id -u` for the account and skips it with a message when the host has none.
- **Directory.** The role creates `~/.ssh` at 0700, as the account, through `become_user`, so no file under the home belongs to root. Homes live under `/Users` on macOS and `/home` elsewhere.
- **Key.** When neither `id_ed25519` nor `id_ed25519.pub` exists, the role runs `ssh-keygen -t ed25519` with an empty passphrase and the comment `<name>@<host>`. `creates:` keeps a rerun from regenerating it. The role never replaces a key file, and it never edits `authorized_keys` or touches a private key.
- **Known hosts.** The role copies the repository's `ansible/known_hosts`, which pins every host under its inventory name, to `~/.ssh/known_hosts_tbhb` at 0644.
- **Configuration.** A managed block marked `ssh_host_keys` in `~/.ssh/config` (created at 0600 when missing) holds one `Host` line naming every other agent host and the Windows hosts left out, with `IdentityFile ~/.ssh/id_ed25519`, `IdentitiesOnly yes` and `UserKnownHostsFile ~/.ssh/known_hosts_tbhb`. The names come from `groups['agent_hosts'] | difference(groups['windows'] | default([])) | difference([inventory_hostname])`, so host-d is in the list.
- **Mesh configuration.** When `~/.ssh/agent-mesh.conf` exists, `replace` (with a backup) limits substitutions to the lines `IdentityFile ~/.ssh/agent-mesh/agent_ed25519` to `IdentityFile ~/.ssh/id_ed25519`. Every other line stays, the `operator_ed25519` ones included.
- **Report.** The role reads the `.pub` file and prints `<name>@<host>: <public key>`. It never reads or prints the private key.

Check mode reports a missing key as changed and doesn't run `ssh-keygen`. The report then says `no public key yet` for that account.

## After the run

The role limits additions to keys and ssh client configuration. @tbhb commits the printed public halves and wires them into `authorized_keys` in a later change.

## Verification scope

The Ubuntu Molecule scenario uses real `ssh-keygen`. It checks the owners, modes and comment of a new key, that the public key matches the private key, that an account with an existing key pair keeps it, and that a missing account gets nothing. It hasn't run on macOS.
