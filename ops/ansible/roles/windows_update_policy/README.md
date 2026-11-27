---
type: Readme
title: The Windows update policy role
description: This role keeps Ubuntu running under the Windows Subsystem for Linux and schedules Windows updates on desktop-a.
tags: [ansible, windows, wsl, updates]
status: stable
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:01890afe1626b9c6
    at: "2026-11-11T17:27:02Z"
---

<!-- cspell:words HKLM -->

The `windows_update_policy` role manages the Windows Subsystem for Linux (WSL) watchdog and Windows Update policy on desktop-a. `meta/argument_specs.yml` documents the options, and `playbooks/windows-update-policy.yml` applies the role to the `windows` group. `Ansible` connects as the operator over ssh into an elevated PowerShell session.

## Guest watchdog

The `Start WSL Ubuntu` task at `\` runs `C:\Windows\System32\wsl.exe -d Ubuntu --exec /bin/true` as tony. Its principal uses S4U logon with Limited privileges, without storing a password. A boot trigger starts the task after 30 seconds. Every five minutes, a repeating trigger runs the task indefinitely, using a fixed start boundary in the past without a duration or end boundary. Installation success event 19 from `Microsoft-Windows-WindowsUpdateClient` on the System log also starts the task after one minute.

IgnoreNew prevents overlapping runs. Execution has a five-minute limit, and battery operation doesn't skip or stop the task. When Ubuntu is running, the action exits at once.

PowerShell requires an existing task with the tony/S4U/Limited principal and the watchdog action. A missing task or a mismatch fails before any write. The scheduled task module requires a password for S4U, so `ansible.windows.win_powershell` compares triggers and edits them through `Set-ScheduledTask -Trigger` without credentials or task registration. It preserves the principal, action, and other settings. Settings drift causes a separate `Set-ScheduledTask -Settings` call that limits changes to the four managed fields. Check mode reports changes without writing.

Script parameters come from the `windows_update_policy_wsl_subscription`, `windows_update_policy_wsl_interval`, `windows_update_policy_wsl_boot_delay`, `windows_update_policy_wsl_event_delay`, and `windows_update_policy_wsl_start_boundary` defaults. Read-back checks the documented policy values.

## Update timing

Quality updates have a three-day deferral, and feature updates have a 365-day deferral. Inventory can override the defaults when the operator chooses feature update timing. A 365-day deferral doesn't pause feature updates indefinitely.

Automatic updates stay enabled, with automatic downloads and scheduled installation on Sunday at 04:00 local time. On desktop-a, the time zone is Eastern Standard Time, which observes daylight saving time: 04:00 Eastern is 09:00 Coordinated Universal Time (UTC) in winter and 08:00 UTC during daylight time. Scheduled automatic restarts wait while users remain logged on.

`windows_update_policy_updates` holds the 32-bit registry values under `HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate`, and `windows_update_policy_automatic_updates` holds those under its `AU` key. Each default has a comment describing its effect. Store and Defender policies stay outside this role, along with accounts, firewall rules, and `Tailscale` settings.

## Checks

Windows lacks a Molecule scenario. Check the run before applying:

```bash
mise run playbook -- playbooks/windows-update-policy.yml --limit desktop-a --check --diff
```

The final tasks read the scheduled task XML and both policy keys. Their output shows the observed state, and an assertion fails on a mismatched trigger, principal, action, setting, registry value, or registry type. This read runs even in check mode without changing the host. A check before the first apply fails the assertion because it reports actual state, including changes the check predicts but hasn't applied. After apply, repeat the check to confirm `changed=0` and a matching read-back.
