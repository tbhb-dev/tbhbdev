---
type: Readme
title: The agent account role
description: Manage the agent account, its SSH launch keys, and its Bazel settings.
tags: [ansible, agents, ssh]
status: stable
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:c61d68f50aed260b
    at: "2026-11-23T11:13:59Z"
---

<!-- cspell:words chgrp -->

`agent_account` manages the `agent` account and the keys in `agent_account_keys`. Inventory adds the lab launch key on lab nodes and one exec launch key per controller on Linux and macOS agent hosts. Each launch line contains `ssh-ed25519`, the public key, and its comment. It excludes leading whitespace, `restrict`, `command=` or other options. Launch keys can open a shell or run commands as `agent`.

An empty `agent_exec_launch_keys` mapping doesn't add exec launch lines. Separate from the lab launch key, the existing controller key pairs remain available. The role maintains `authorized_keys` as an exclusive list.

## Linux account migration

On Linux, the account has user ID 24368 and its own primary group, `agent`, with group ID 24368. Group ownership of the home and deployment configuration uses that ID. An existing account with primary group ID 33835 migrates without moving its home or replacing its supplementary groups. The role limits changes to paths carrying the `devel` group ID inside the home and workspace, using `find -P` and `chgrp -h` without following symlinks. Set `agent_workspace_root` to override the workspace path. Otherwise the role resolves the configured workspace path relative to the home, using `Code` by default. File ownership by other groups stays the same. Removal of `devel` requires an empty group with no primary or supplementary members.

Before migration, task output records the old primary group and its ID, the previous `agent` group record, and the name, ID, remaining members, and matching paths for the legacy group. The removal task logs the group record again. Preserve the apply output for rollback. To restore host groups after reverting the PR, use the recorded primary group, recreate `devel`, and restore file groups with `chgrp -h`. In check mode the role previews the migration. An immediate second apply reports `changed=0`.

On macOS, the role preserves account behavior, preflight checks, and the existing `devel` primary group. Controller launch-key delivery outside the agent home keeps its existing operator ownership and group. Inside the agent home, the role migrates keys with the legacy group ID along with the other files.

## Bazel settings

`agent_account` replaces the agent's entire `~/.bazelrc` and creates `~/.cache/bazel-disk` with mode `0700` under the agent's home. Checkouts on the host share this cache. The Bazel server uses a 3 GiB heap and exits after 900 idle seconds.

Set `agent_account_bazel_resource_fraction` to change the default fraction of `.25` for CPU, memory, and job limits. Garbage collection uses a size limit of `50G` and an age limit of `7d` for unused entries. Configure those limits with `agent_account_bazel_disk_cache_max_size` and `agent_account_bazel_disk_cache_max_age`. Lab Macs in the `lab_macos` inventory group use `20G`. A host variable can override that size.

## Tests

Molecule runs scenarios for a fresh account, migration of an existing account, a user ID clash, and a group ID clash. The existing-account scenario checks that check mode preserves the fixture, convergence migrates it, a repeat run leaves the account unchanged, outside paths and symlink targets keep their old groups, and a shared `devel` group survives. It also checks the rendered Bazel settings and the disk cache directory's ownership and mode.

## Known hazard

The role owns `the account’s tbhbdev configuration file` and removes lines added by hand on the next apply. Put any required line in `agent_account_tbhbdev_config` or its overrides. By default, the role renders all five references to credentials for the review and pull request apps on `unix_agent_hosts`. Use `agent_account_config_credentials_overrides` to replace an individual reference.
