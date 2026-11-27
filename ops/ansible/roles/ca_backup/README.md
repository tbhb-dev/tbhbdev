---
type: Readme
title: CA backup on the storage host
description: Stage and install the CA backup programs for scheduled runs as root on the storage host.
tags: [ansible, ca, backup]
status: draft
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: medium
    surface: exec
    session: sha-256-64:c55d6dc903f6d319
    at: "2026-11-25T16:37:24Z"
---

<!-- cspell:words mktemp -->

Run the [backup playbook](../../playbooks/ca-backup.yml) from the merged revision. Inventory connects as `ops`. Before staging, the role checks `/usr/local/bin/python3.11` and stages the programs and `SHA256SUMS` in `/srv/example-storage/docker/ca-backup-staging`. Set parent directory permissions to allow `ops` to create the staging directory.

Backups exclude `ts-state/`. A restored CA host rejoins the `Tailscale` network through first enrollment. Follow the CA deployment instructions in the companion.

## Root hand-off from `admin-vm`

1. On `admin-vm`, in a trusted repository checkout, set `merged_sha` to the full merged commit verified on `origin/main`. Generate the expected manifest directly from that commit. Transfer it separately to `/root/ca-backup.SHA256SUMS` on `nas-a` over the administrator connection.

   ```bash
   set -euo pipefail
   umask 077
   git merge-base --is-ancestor "$merged_sha" origin/main
   for name in backup.py status.py; do
     digest=$(git show "$merged_sha:apps/ca/backup/$name" | sha256sum)
     printf '%s  %s\n' "${digest%% *}" "$name"
   done > ca-backup.SHA256SUMS
   ```

2. As root on `nas-a`, verify root owns `/srv/example-storage/docker/ca-backup.key` and the file has mode `0600`. Confirm the CA project path and that OpenSSL supports `-pbkdf2`. Copy the staged files into a private root directory and verify those copies before installing them. Check that the trusted manifest matches the staged manifest.

   ```sh
   set -eu
   umask 077
   stage=/srv/example-storage/docker/ca-backup-staging
   state=/var/lib/ca-backup
   archives=/srv/example-storage/docker/ca-backup/archives
   mkdir -p "$state" "$archives"
   chown root:root "$state" "$archives"
   chmod 0700 "$state" "$archives"
   pending=$(mktemp -d "$state/install.XXXXXX")
   (
     cd "$pending"
     cp "$stage/backup.py" "$stage/status.py" "$stage/SHA256SUMS" .
     cmp /root/ca-backup.SHA256SUMS SHA256SUMS
     sha256sum -c SHA256SUMS
     chmod 0700 backup.py status.py
     mv -f backup.py status.py "$state/"
     rm SHA256SUMS
   )
   rmdir "$pending"
   [ -f "$state/monitor-start" ] || touch "$state/monitor-start"
   ```

3. On `nas-a`, turn off any older backup schedule and configure the root `cron` job to run daily at 03:15 host time. Use the script below. After each backup attempt, the script runs the status commands even if the backup fails. A failed backup or status command produces a nonzero exit status. Publish to the `Tailscale` service address for `svc:otlp` because the resolver on the `Synology` host lacks MagicDNS support.

   ```sh
   export PATH=/usr/local/bin:/usr/bin:/bin
   umask 077
   result=success; /usr/local/bin/python3.11 /var/lib/ca-backup/backup.py --project /srv/example-storage/docker/ca --destination /srv/example-storage/docker/ca-backup/archives --key-file /srv/example-storage/docker/ca-backup.key || result=failure
   SERVICE_RESULT="$result" /usr/local/bin/python3.11 /var/lib/ca-backup/status.py record --state /var/lib/ca-backup && /usr/local/bin/python3.11 /var/lib/ca-backup/status.py publish --state /var/lib/ca-backup --endpoint http://192.0.2.55:4318/v1/metrics && [ "$result" = success ]
   ```
