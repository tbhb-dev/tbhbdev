---
type: Readme
title: Production object store
description: Compose project for the RustFS interim store.
tags: [s3, rustfs, tailscale, docker]
status: draft
generated:
  - role: generated
    harness: claude-code:2.1.322
    model: anthropic:claude-sonnet-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    at: "2026-11-14T23:37:36Z"
---

<!-- cspell:words rustfs tbhbops -->

Ops' read of the storage server on 2026-11-14 found the same Compose file as the [`s3` project](compose.yaml), apart from the process limit and logging settings added on both services and different comments in this copy. Project `s3` and service `rustfs` give the container its name, `s3-rustfs-1`. The image is `rustfs/rustfs:1.0.4`, pinned by digest. Ops' read of the live host on 2026-11-14 found the same digest pin on the running container.

## Modes

Interim mode is the default. It serves plain HTTP on TCP 9000, with access restricted to `Tailscale`. The store binds `S3_BIND_ADDR`, the host's `Tailscale` address. On the storage server, that value comes from `compose.env`, and ops' read of the live host on 2026-11-14 found the running container publishing TCP 9000 on that address. The live `compose.env` defines one key, `S3_BIND_ADDR`, with mode 600, and the repository omits the value.

Without `S3_BIND_ADDR`, Compose uses the local host address `127.0.0.1`. A wildcard value would break the rule restricting access to `Tailscale`. `packages/tbhbops/tests/test_s3_compose.py` checks the Compose default for a wildcard. No check rejects a wildcard in `compose.env`, and the operator checks that file before `up`.

Setting `S3_TLS=1` switches the store to Transport Layer Security (TLS) with the `*.s3.example.invalid` certificate from the `lego` service and, by convention, the operator also sets `S3_BIND_ADDR`. Compose can't enforce that pairing. The `lego` service belongs to the `cert` profile. A weekly DiskStation Manager (DSM) task runs it, and `up` never starts it. With `RUSTFS_TLS_RELOAD_ENABLE` on in the compose file, RustFS re-reads `./tls` every 30 seconds (the default `RUSTFS_TLS_RELOAD_INTERVAL`) and serves a renewed certificate with no restart. Version 1.0.4 leaves it off by default, and it does nothing in interim mode.

## Start after a restart

Docker can try to start RustFS before Tailscale assigns `S3_BIND_ADDR`. A failed bind leaves RustFS stopped. At boot, the task checks for the address every five seconds and starts RustFS once the address appears. After 60 failed attempts, it logs an error and exits with a nonzero status.

If `docker compose version` succeeds, the script uses `docker compose`. Otherwise, it looks for `docker-compose` on `PATH`. With neither command available, the script reports an error and exits at startup.

- Copy the boot script beside the deployed `compose.yaml` and `compose.env`. [Boot script](start-on-boot.sh)
- In DSM Task Scheduler, choose **Create > Triggered Task > User-defined script**. Select **root** as the user and **Boot-up** as the event.
- Enable the task and enter the command below under **Task Settings**. Replace `/path/to/s3` with the deployed directory.

```sh
/bin/sh /path/to/s3/start-on-boot.sh
```

## Settings

The `S3_*` variable names belong to this compose file: `S3_BIND_ADDR`, `S3_TLS`, `S3_PEER_NAME`, `S3_PEER_ADDR`, `S3_OBS_ENDPOINT` and `S3_ACME_EMAIL`. They start fresh with this file rather than extend an earlier naming convention. `RUSTFS_UNSUPPORTED_FS_POLICY=fail` stays, and RustFS keeps its console off.

## Paths

Compose resolves these paths from the project directory and mounts the credential files with `:ro`.

| Use | Path |
| --- | --- |
| RustFS data | `./data`, mounted at `/data`. |
| Certificate directory | `./tls`, mounted at `/tls`. |
| State for the `lego` service | `./lego`. |
| Root access key | `./secrets/rustfs.access_key`, mounted at `/run/s3/rustfs.access_key`. |
| Root secret key | `./secrets/rustfs.secret_key`, mounted at `/run/s3/rustfs.secret_key`. |
| DNS token for the `lego` service | `./secrets/lego-dns-token`, mounted at `/run/s3/lego-dns-token`. |

User and group ID 10001 runs the container, and the project directories use that owner with mode 700. A missing key file stops Compose because those binds don't create a host path. The `ops` account can't read the `secrets/` directory. Use the Compose file for the recorded names. File presence and modes remain unverified.

Inside the container, a health check requests `/health/ready` over HTTPS first, then over HTTP. The service drops all capabilities and prevents new privileges. A 2 GiB limit caps its memory. Because the receiver rule proposed in tbhb-dev/tbhbdev#358 requires them, both services set a process limit and use `json-file` logging with `max-size` and `max-file`, and the live container lacks those additions until its next recreate.

## Tooling scope

`packages/tbhbops/src/tbhbops/s3_store.py`, `packages/tbhbops-cli/src/tbhbops_cli/s3.py` and the `render-identities` command in that module manage SeaweedFS and leave this RustFS project untouched.

This project establishes no replication, and it has no command-line tool for deployment. Deploy through reviewed and merged git, with an undo procedure recorded first.
