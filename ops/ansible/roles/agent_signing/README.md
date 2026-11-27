---
type: Readme
title: The agent signing role
description: Build ssh-sign on the controller, install it, and create a missing signing key for the agent account, ahead of the harness role.
tags: [ansible, agents, signing]
status: draft
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:e623fec292006b9a
    at: "2026-11-14T08:22:27Z"
---

`agent_signing` builds `ssh-sign` on the `Ansible` controller and prepares the files `agent_harness` requires and never creates: `.local/bin/ssh-sign`, and the signing key `.config/devtools/agent-signing-key.pub` with its private half. It writes the files as `agent` so the account owns them. `playbooks/agent-signing.yml` runs it on `linux_agent_hosts`. Run that playbook once per host, before `agent-harness.yml`. It's a separate playbook because a new key needs a human step afterwards, and because `agent-harness.yml` also targets the operator's Macs, where 1Password supplies the keys.

## Steps for the agent account

- **ssh-sign.** The tool is Go source in example-org/devtools-example, a private repository that publishes tags and no binaries, and the hosts have neither Go nor credentials for it. The controller builds it instead, once per OS and architecture among the play's hosts, and the role copies the result. The controller is mac-a, mac-b or host-f, with Go from mise and a clone at `agent_signing_devtools_src`, which defaults to `~/Code/github.com/example-org/devtools-example`. The role exports `agent_signing_devtools_version` from that clone with `git archive` into a temporary directory, which leaves the operator's checkout unchanged. It then runs `go build -trimpath` with `CGO_ENABLED=0` and the `GOOS` and `GOARCH` of the host's facts, and removes the directory at the end. It fails with a message when the clone or the tag is missing, and never fetches from GitHub. The role copies the build to `.local/bin/ssh-sign` at 0755 for the account, and **replaces an installed program whenever its checksum differs from the build**. A new tag in `agent_signing_devtools_version` upgrades each host on the next run, and an unchanged tag leaves the program alone. Earlier versions of this role never replaced the program. The build also runs in check mode, because its writes stay in a temporary directory on the controller, so check mode reports the copy truly. It needs the Go module dependencies, which Go takes from its own cache or proxy.
- **Key.** When neither `agent-signing-key` nor `agent-signing-key.pub` exists, the role runs `ssh-keygen -t ed25519` with an empty passphrase, as the account, in a 0700 `.config/devtools`. The role creates the key on the host at 0600, and it stays there. The role never reads, copies or prints it, and `creates:` keeps it from replacing one. A public key without a private one belongs to a key in 1Password, and the role leaves it alone. A private key without its public half stops the run, and the operator can recover the public half with `ssh-keygen -y`.
- **Settings.** When the host stores the private key, the role writes `.config/devtools/signing.env` at 0600 with the `file` provider and both key paths. Without it, `ssh-sign` defaults to the 1Password provider and another public key name. The role keeps an existing `signing.env`.
- **Git identity.** For an account with a file key, the role writes `~/.gitconfig` with the `agent_signing_git_name` and `agent_signing_git_email` identity, `gpg.format = ssh`, the public key as `user.signingkey`, `ssh-sign` as `gpg.ssh.program` and `commit.gpgsign = true`, replacing any other content. The role leaves settings for an op-style account alone.
- **Report.** For an account with a file key, the role prints the public key.

Check mode reports the plan and doesn't create anything.

## Register each new public key

Sign in to GitHub as `tbhb-agent` and add the public key printed by the play as a signing key under `github.com/settings/keys`. Register one key per host. Repeat registration after a rebuild because GitHub marks commits signed with an unregistered key as unverified. Then run `agent-harness.yml`.

## Verification scope

The scenario uses `ssh-keygen` on Ubuntu and Go and Git on the controller. Its test repository contains the pinned tag, a later commit that builds a program which fails, and an untracked file. The installed program must run. Molecule checks file ownership and modes, then verifies that `signing.env` points to a matching key pair. It also checks preview and repeat runs, then replaces the program to confirm that the role restores the build.
