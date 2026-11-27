---
type: Readme
title: Purge the agent mesh
description: Remove the agent mesh directory, configuration, known hosts and Include lines from the agent and ops accounts, after proving the per-host keys.
tags: [ansible, agents, ssh]
status: draft
generated:
  - role: generated
    harness: claude-code:2.1.319
    model: anthropic:claude-opus-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    at: "2026-11-10T02:54:15Z"
---

`agent_mesh_purge` handles each account in `agent_mesh_purge_users`, which defaults to `agent` and `ops`. Each account ends with no agent mesh file. It retains its own `~/.ssh/id_ed25519`, `~/.ssh/known_hosts_tbhb` and the `ssh_host_keys` block in `~/.ssh/config` remain for host-to-host logins. `playbooks/agent-mesh-purge.yml` runs it on `agent_hosts:!windows`, with become. The storage servers aren't in that play.

## Steps

- **Account.** The role runs `id -u` for each account and skips one the host doesn't have, with a message. Homes live under `/Users` on macOS and `/home` elsewhere.
- **Proof.** Before any removal, for every account on the host, the role runs `ssh -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none -o ConnectTimeout=10 -i ~/.ssh/id_ed25519 -o UserKnownHostsFile=~/.ssh/known_hosts_tbhb <account>@<host> true` as that account, once for each other agent host. The peers are `agent_mesh_purge_peers`, which defaults to the same list as `playbooks/ssh-keys-proof.yml`. The role fails the host when any login fails, and every account on it keeps its files. `agent_mesh_purge_require_proof` turns the proof off. An account with no `~/.ssh/id_ed25519` fails the host the same way, because its keys are unproven. The ssh task runs in check mode too and never reports a change.
- **Files.** As the account, through `become_user`, the role removes `~/.ssh/agent-mesh/` with everything in it, `~/.ssh/agent-mesh.conf`, `~/.ssh/agent-mesh_known_hosts`, and every `~/.ssh/agent-mesh.conf.*~` backup. The `operator_ed25519` blocks in `agent-mesh.conf` go with the file.
- **Include lines.** The role removes each line of `~/.ssh/config` that matches `^\s*Include\s+.*agent-mesh`. Every other line stays.

The role never reads or changes `id_ed25519`, `known_hosts_tbhb` or the `ssh_host_keys` block. A rerun on a purged host makes no further change.

## Variables

- `agent_mesh_purge_users`: accounts to purge. Default `[agent, ops]`.
- `agent_mesh_purge_require_proof`: run the proof first. Default `true`.
- `agent_mesh_purge_peers`: hosts the proof logs in to. Default is every agent host except this one and the Windows hosts, and the operator Macs when this host belongs to that group, because the `Tailscale` policy closes their port 22 to the lab nodes. A lab node limits its attempts to the other lab nodes, because the other agent hosts refuse keys from lab nodes (`ssh_host_keys_refused` in `group_vars/agent_hosts.yml`).

## Verification scope

The Ubuntu Molecule scenario turns the proof off. It checks that the role removed the mesh directory, files, backups and Include lines, and that `id_ed25519`, `known_hosts_tbhb` and the managed block stay. It hasn't run on macOS, and the proof has no scenario.
