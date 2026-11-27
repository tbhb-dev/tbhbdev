---
type: Readme
title: Refresh agent settings
description: Schedule a refresh of agent account settings on Linux and macOS.
tags: [ansible, agents]
status: stable
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: medium
    surface: exec
    session: sha-256-64:190de70b65282e1a
    at: "2026-11-25T02:16:32Z"
---

<!-- cspell:words bootout reconverge tomllib -->

`agent_reconverge` schedules the existing `tony/reconverge.sh --account agent` job from the account configuration repository on the agent hosts. Run it after `agent_account` with an existing Linux or macOS account. It never clones, fetches, pushes, or creates that checkout. A missing script logs a debug message. Scheduling still proceeds.

On failure the job logs its error and leaves settings unchanged.

| Input | Default | Meaning |
| --- | --- | --- |
| `agent_reconverge_account` | `agent` | Account that runs the job |
| `agent_reconverge_clone` | From inventory | Clone path relative to the account home |
| `agent_reconverge_interval_seconds` | `900` | Positive interval in seconds |
| `agent_reconverge_macos_python_version` | `3.13.18` | Full `CPython` version for the macOS daemon |
| `agent_reconverge_macos_python_url` | python-build-standalone release `20261124` | arm64 macOS `install_only` archive URL |
| `agent_reconverge_macos_python_sha256` | Release `SHA256SUMS` entry | Archive digest checked before extraction |

## Linux timer

Linux uses `~/.config/systemd/user/reconverge-agent.service` and `reconverge-agent.timer`, owned by the account with mode `0644`. The role enables linger and starts the user manager. This lets the timer run without a login. By default, jobs start one minute after activation and repeat every 15 minutes. Exit codes 0 and 1 count as success. The job uses 1 when the settings file changes. Unit changes reload the user manager and restart the timer.

## macOS daemon

macOS uses the root-owned `0644` LaunchDaemon `/Library/LaunchDaemons/dev.tbhb.reconverge.agent.plist` in the system domain. It sets `UserName` to the account and `WorkingDirectory` and `HOME` to its home. Putting `/Library/tbhb-python/bin` before `/opt/homebrew/bin:/usr/bin:/bin` gives the job a `python3` with `tomllib`. The job runs at load and every 900 seconds by default. On apply, launchd reloads the daemon if the property list changed. An unloaded daemon starts on apply.

The role verifies the archive's checksum and extracts it to `/Library/tbhb-python/3.13.18/python` as root. It checks that the extracted tree is root-owned and has no group or world writable files or directories before linking it into the daemon's PATH and at `/usr/local/bin/python3.13`. Update the version, URL, and digest together when changing the pin. Check mode skips the install.

## Preview and removal

In check mode, the role shows diffs for the files. It reads account and job state without starting or reloading jobs. If `agent_account` plans a new account, this role reports the pending installation and stops before account lookup. The role leaves the clone alone in all modes.

To roll back, turn off the Linux user timer and stop it as the account or unload the macOS daemon with `launchctl bootout system/dev.tbhb.reconverge.agent`. Remove the unit files to keep a later boot from loading them, or revert this change before the next `Ansible` apply.
