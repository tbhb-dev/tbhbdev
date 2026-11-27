---
type: Readme
title: The ops tools role
description: Install the ops account tools and their Connect settings on the operator's Macs.
tags: [ansible, ops, tbhbdev]
status: draft
generated:
  - role: generated
    harness: claude-code:2.1.322
    model: anthropic:claude-sonnet-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    subagent: sha-256-64:7f56e34c196dcb92
    at: "2026-11-13T21:36:43Z"
---

<!-- cspell:words tbhbops -->

`ops-tbhbdev.yml` runs the role on `operator_macs`. `group_vars/operator_macs.yml` connects as `ops`, and every task runs as `ops` in the `ops` home without `become`. The role gives `ops` its own `tbhbdev`. That lets `ops` run `tbhbdev secret read` with the ops-controller Connect token that `tbhbops connect token-commands` stores in the `ops` keychain. The [agent workspace role](../agent_workspace/README.md) describes that token.

## Tasks

1. The `uv` installer requires `/opt/homebrew/bin/mise` and runs `mise install uv@<version>` when `mise where` reports that version missing. `ops_tbhbdev_uv_version` defaults to `0.13.6`. Mise stays inactive for `ops`, and the `PATH` is the same as before.
2. `tbhbdev`: the role reads `direct_url.json` from the dist-info under `~/.local/share/uv/tools/tbhbdev` and compares `vcs_info.commit_id` with `ops_tbhbdev_commit`, which defaults to commit `3bf88eef651da2b8a56b3c7fd82ffc9032828c84` of `tbhb-dev/tools`. Any other record, or none, runs `mise exec uv@<version> -- uv tool install --reinstall git+https://github.com/tbhb-dev/tools@<commit>`. That task reports `changed` for `tbhbdev` when it installs the tool. Pin a full 40 character commit, because the comparison is exact.
3. The `tbhbdev` configuration file gets `[connect] endpoints` from `ops_tbhbdev_connect_endpoints`, which the inventory sets for `operator_macs`. The file has mode 0600 in a 0700 directory. `tbhbdev secret read` requires one configuration key. It reads the token from the keychain `ops-connect.keychain` under service `ops-connect` and account `agent`, which the role leaves alone.

`uv tool install` links `tbhbdev` into `~/.local/bin`, and the `PATH` of an `ops` ssh session doesn't include that directory. Call `~/.local/bin/tbhbdev`, or add the directory to the `PATH` of the caller. The package needs Python 3.15 or later. On the first install `uv` fetches its own Python from GitHub.

Short Connect host names expand at play time using `MagicDNSSuffix` from `tailscale status --json` on the target, including in check mode. `Tailscale` must be running and return a nonempty suffix. Full host names are unchanged and don't require a `Tailscale` lookup. Inventory supplies the full name. `Tailscale` status, derived URLs, and configuration rendering use `no_log` so the suffix stays out of output and diffs. The full name matches the `Tailscale` Serve Transport Layer Security (TLS) certificate. Short names are `https://` plus one DNS label, with an optional `:port` kept after the suffix. Anything else, such as a path, user information, or a full name, passes through unchanged. Set `connect_endpoints_tailscale` to the `tailscale` binary's path when it isn't on the play's PATH. It defaults to `tailscale`, and `operator_macs` sets it to the `Tailscale` app's binary.

## Download checks

Mise maps the bare tool name `uv` to `aqua:astral-sh/uv`. For that package the aqua backend applies whatever verification the aqua registry declares, which can include checksums, signatures, and attestations, and mise enables the signature and attestation checks by default (`aqua.cosign`, `aqua.slsa`, `aqua.github_attestations`). This repository's `mise.lock` records `provenance = "github-attestations"` and a sha256 for `uv` 0.13.6, so that's the check in play.

The role leaves the settings at their defaults, which a user-level mise setting on the Mac can weaken. It pins no archive digest for `ops`. `uv tool install` verifies no signature, and it checks that git resolves the pinned commit.

## Check mode

Check mode reads state and leaves the Mac alone. The `mise where` lookup, the `direct_url.json` read and the mise stat run as usual. Both install commands skip, so a `--check` run can't show a pending `uv` or `tbhbdev` install. The directory and the configuration template report their own changes without a diff.

## Verification scope

Molecule runs the role with a macOS platform fixture in a Linux container. Fixtures replace mise and `Tailscale`. The scenario checks mixed short and full endpoint names against a placeholder suffix and reapplies with full names after removing `Tailscale`. It checks configuration permissions and unchanged state on repeat runs. Real macOS installs and Connect access remain unverified.
