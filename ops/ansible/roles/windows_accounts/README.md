---
type: Readme
title: The Windows accounts role
description: This role creates the local agent and ops accounts on a Windows agent host, with ops in Administrators and every login by ssh key.
tags: [ansible, windows, accounts, openssh]
status: draft
generated:
  - role: generated
    harness: claude-code:2.1.322
    model: anthropic:claude-sonnet-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    subagent: sha-256-64:9e715381955eb5e0
    at: "2026-11-14T01:19:28Z"
---

The `windows_accounts` role gives a Windows agent host the same two local accounts as the other agent hosts, `agent` and `ops`. The `ops` account joins Administrators, which on Windows plays the part of `sudo` without a password. The `agent` account joins no administrative group. `meta/argument_specs.yml` documents the options, and `playbooks/windows-accounts.yml` applies the role to the `windows` group, which holds desktop-a.

## Connection

`Ansible` reaches the host over ssh with `ansible_shell_type: powershell`, and the Windows modules run as PowerShell without any Python on the host. An administrator's ssh session starts elevated, which makes `become` unnecessary. `group_vars/windows.yml` connects as the operator until a later change points it at `ops`.

## What it changes

- Creates the `agent` and `ops` users with a random password that nobody reads. An existing account keeps its password, because the role sets the password at account creation. Both accounts never expire and stay enabled. The console and Remote Desktop password logons stay denied for both, while key logon over ssh works.
- Adds `ops` to Administrators and `agent` to Users. It removes `agent` from Administrators if something added it there. Other groups `agent` holds, such as `docker-users`, stay as the host has them.
- Adds each `ops` key from `windows_accounts_ops_admin_keys` to `C:\ProgramData\ssh\administrators_authorized_keys`, one line per entry. The host's `sshd_config` sends every member of Administrators to that file, so `ops` gets its login there. The file holds other people's keys and the role doesn't own it. The role appends each line if no identical line exists and leaves the other lines intact. It doesn't use a `regexp` or an exclusive write, and never templates the whole file. It leaves the file's permissions alone, so the file has to exist already: the role fails instead of creating one, because a new file would get the wrong permissions. The module rewrites the file with `newline: windows`, so CRLF lines keep their endings and a line with LF endings becomes CRLF.
- Creates the profile of `agent`, which a new account lacks until its first logon, and writes `.ssh\authorized_keys` there from `windows_accounts_agent_keys`. The permissions on that file allow `agent`, Administrators, and SYSTEM. Windows can add a suffix to the profile folder, as in `agent.000` on desktop-a. The role reads the path from the registry by the account's security identifier and fails if the registry has none.

Windows has no user IDs. The host assigns each account a security identifier, so the fixed IDs of the Linux and macOS roles don't apply.

## Keys

Both `windows_accounts_agent_keys` and `windows_accounts_ops_admin_keys` hold entries with a `key` and an optional `from` list of hosts on the `Tailscale` network. Each name goes through the same `authorized_keys.j2` template and address resolution filter on the controller at apply time, and the role writes the addresses into a `from=` option. Git holds names without addresses, and `sshd` on Windows matches an address in `from=` but not a name. A name that doesn't resolve into the `Tailscale` address range fails the run.

The role replaces the key file for the agent account. For ops, it adds each line and keeps the existing lines for administrators. Both lists refuse an empty list and keys with options or more than one line. Ops requires ed25519 public keys. In the inventory, the ops list holds references to every `ssh_controller_keys` public key in `group_vars/all.yml`, each with a `from` entry restricted to its own host, then the operator's own login key from `operator_login_keys` as a plain entry with no `from`. The ops list refers to those maps for the key text. A test checks that the references match the controller map.

## Checks

Every assertion runs before the first change. The host must be Windows. The lists need at least one key apiece. Keys occupy one line without options. Ops accepts ed25519 keys, and `from` entries use plain host names. Ops has no Molecule scenario for Windows, so check a run first:

```bash
mise run playbook -- playbooks/windows-accounts.yml --limit desktop-a --check --diff
```

### Console check for the ops keys

Each ops key task iteration rewrites `administrators_authorized_keys` through a temporary file, which could change line endings or the file's permissions. Around the first apply on a host, run this inspection at the console in an elevated PowerShell window.

Before the apply, record the file and keep the window open:

```powershell
$f   = 'C:\ProgramData\ssh\administrators_authorized_keys'
$keys = @('<controller key body>', '<controller key body>', '<controller key body>')
$b0  = [IO.File]::ReadAllBytes($f)
$t0  = [Text.Encoding]::UTF8.GetString($b0)
"bytes=$($b0.Length) crlf=$(([regex]::Matches($t0,"`r`n")).Count) lf_only=$(([regex]::Matches($t0,"(?<!`r)`n")).Count) ends_with_newline=$($t0.EndsWith("`n"))"
(Get-FileHash $f -Algorithm SHA256).Hash
(Get-Acl $f).Sddl
icacls $f
```

Replace the placeholders in `$keys` with the base64 field of each key in `windows_accounts_ops_admin_keys`, following the controller references to `ssh_controller_keys` in `inventory/group_vars/all.yml`.

After the apply, in the same window:

```powershell
$t1 = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($f))
$keys | ForEach-Object {
    $key = $_
    "key=$key lines=$((($t1 -split "`r`n|`n") | Where-Object { $_.Contains($key) }).Count)"
}
$rest0 = $t0
$rest1 = $t1
foreach ($key in $keys) {
    $pattern = "(?m)^.*$([regex]::Escape($key)).*(`r`n|`n)?"
    $rest0 = $rest0 -replace $pattern, ''
    $rest1 = $rest1 -replace $pattern, ''
}
$norm  = { param($s) ($s -replace "`r`n", "`n").TrimEnd("`n") }
"other_lines_identical=$((& $norm $rest0) -ceq (& $norm $rest1))"
"crlf=$(([regex]::Matches($t1,"`r`n")).Count) lf_only=$(([regex]::Matches($t1,"(?<!`r)`n")).Count)"
(Get-Acl $f).Sddl
icacls $f
```

- Each expected key reports `lines=1`.
- `other_lines_identical` is `True`. It excludes the managed keys from both reads and compares every other line character for character, ignoring line endings and the final newline, which `newline: windows` rewrites.
- When the first read showed `lf_only=0` and `ends_with_newline=True`, `$t1.StartsWith($t0)` is also `True`, a byte-for-byte prefix match.
- The `Get-Acl` security descriptor equals the one from the first read. If it differs, stop, because the SSH server may ignore a file with the wrong permissions, and remove the lines this role added at the console to roll back.

Last, repeat the check run from the preceding block, which reports `changed=0` for the host and `ok` for every ops key iteration once the lines are in place.
