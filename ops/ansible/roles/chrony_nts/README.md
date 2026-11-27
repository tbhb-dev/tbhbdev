---
type: Readme
title: Time sources with Network Time Security
description: Add Network Time Security (NTS) to three lab hosts through a configuration fragment.
tags: [ansible, chrony]
status: draft
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:492a2591ba326dce
    at: "2026-11-14T19:13:15Z"
---

<!-- cspell:words chrony chronyc -->

Run the `lab-chrony-nts.yml` play against host-a, host-b, and host-c. Preview the change with `mise run playbook -- playbooks/lab-chrony-nts.yml --check --diff`. After review and merge, omit `--check` to apply it.

Install `chrony` before running the role. To prepare for the fragment, the role checks that `/etc/chrony/conf.d` exists and that the main configuration reads it through `confdir /etc/chrony/conf.d` or `include /etc/chrony/conf.d/*.conf`. These checks also run in check mode. A read of the main configuration confirms the include without changing any packaged source file.

For its managed fragment, the role writes `/etc/chrony/conf.d/10-cloudflare-nts.conf`. This fragment adds Cloudflare as a source beside Ubuntu's stock sources and doesn't set `prefer`, so Cloudflare isn't the preferred source. It gives root ownership to the file with mode `0644` and sets the following contents:

```text
# Managed by ansible role chrony_nts
server time.cloudflare.com iburst nts
```

Normal runs restart Ubuntu's `chrony` service when the fragment changes and `chrony_nts_run` has its default value of `true`. After flushing handlers, the role ensures that `chrony` runs even with an unchanged fragment. Check mode previews the service state without starting or restarting it.

Validation checks where the fragment landed and whether the service is running. The role leaves NTS negotiation, source selection, and clock accuracy unchecked. After the apply, inspect these with `chronyc sources`, `chronyc authdata`, and `chronyc tracking`.

Check egress on each host outside this role. NTS needs two paths from each host to `time.cloudflare.com`: a Transmission Control Protocol (TCP) connection on port 4460 for the key exchange and `UDP` traffic on port 123 for time. A TCP connection on port 4460 to `time.cloudflare.com` succeeded from host-a, host-b, and host-c on 2026-11-14 at 19:08Z (connection without key exchange), and the default outbound firewall policy on each is `ACCEPT`. A rule could still block `UDP` port 123, because the firewall rules need root to read. Read `chronyc authdata` after the apply. To undo the change, remove `/etc/chrony/conf.d/10-cloudflare-nts.conf` and restart `chrony`.

Use `mise run molecule -- chrony_nts` to check convergence, check mode, unchanged state on repeat runs, the rendered content, and file permissions. Check mode must leave the fragment untouched before convergence. Verification also compares the existing fragment's checksum and timestamps around a check mode pass. Containers lack `systemd`, so Molecule sets `chrony_nts_run` to `false` and skips restart and service state checks. Unit tests cover the include checks and ensure that writes target the fragment alone.
