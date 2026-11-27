---
type: Readme
title: The client trust role
description: Install the public step-ca root for lab clients.
tags: [ansible, ca, certificates]
status: draft
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:f258d614121bf9ac
    at: "2026-11-23T03:53:29Z"
---

The `ca_client_trust` role checks the `SHA-256` fingerprint of the public step-ca root in the companion before it installs trust for `https://ca.example.invalid`. Rotate the root by reviewing the replacement certificate and updating the pin in `defaults/main.yml` in the same change.

The [playbook](../../playbooks/ca-client-trust.yml) targets `linux_agent_hosts`, `lab_macos`, and `operator_macs` one host at a time. On Debian-family hosts, it places the root in `/usr/local/share/ca-certificates/` and refreshes the trust bundle through the `update-ca-certificates` handler. These hosts need `ca-certificates` and OpenSSL.

On macOS, the role manages the `agent` and `ops` accounts by default. Set `ca_client_trust_macos_accounts` to select accounts and `ca_client_trust_macos_homes` to supply their inventory homes. The role checks those paths against Directory Services before writing as each account. It exports Apple's built-in roots and appends the step-ca root to `~/.config/tbhb/ca-bundle.pem`. Managed blocks in `~/.zshenv` and `~/.profile` point client tools at the bundle, as does the account's global Git configuration. Node.js loads the standalone root through `NODE_EXTRA_CA_CERTS`.

## System keychain

Keep `ca_client_trust_system_keychain` off until the operator's decision appears in Decisions. Enabling it limits the change to Macs in `operator_macs`. The role matches the `SHA-256` hash in the System keychain and runs `security verify-cert` with the basic policy on the pinned certificate. It skips the add when both checks pass.

If macOS requires a local administrator to approve the addition, run `./tools/install-step-ca-root` from the reviewed checkout on `mac-a` or `mac-b`. The tool checks the pin, current trust, and whether `sudo` needs a terminal before it requests the change. Check mode previews changes to trust stores and account files.

To undo the Debian install, remove the installed certificate and run `update-ca-certificates`. On macOS, remove the two managed certificate files and the marked startup blocks, and restore the account's previous `http.sslCAInfo` setting. If the operator enabled System trust, remove it separately in Keychain Access. The root has no name constraints and can sign for any name.
