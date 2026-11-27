---
type: Readme
title: Network DNS resolver
description: The CoreDNS Compose project that serves the S3 names by query source and three zones on the private network, and forwards other names over DNS over HTTPS.
tags: [dns, coredns, s3, docker]
status: draft
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:fc0537bfcece93f9
    at: "2026-11-14T19:57:39Z"
---

<!-- cspell:words coredns Corefile tailnet tbhbops -->

## What runs and where

The `dns` Compose project runs one CoreDNS container on each of nas-a and nas-b. Coordination records the deployment on 2026-11-13. It records the upgrade from CoreDNS 1.14.7 to 1.15.0 on nas-b at 01:15Z and nas-a at 01:16Z on 2026-11-14. The before and after dig results matched (not verified here). [`compose.yaml`](compose.yaml) pins CoreDNS 1.15.0 by image digest.

## Layout and files

`config/Corefile` in the companion serves `s3.example.invalid` through three blocks checked in order. Source addresses select the view. The `lan` view answers sources in 198.51.100.0/24 and the addresses in `DNS_LAN_TAILNET_ADDRS` from `zones/lan.zone` in the companion, while the `tailnet` view answers `Tailscale` sources from the rendered `zones/tailnet.zone`. Sources that match neither view reach the last block, which refuses the query. `zones/lan.zone` is the source of truth for Local Area Network (LAN) answers. `zones/tailnet.zone.tmpl` holds placeholders for answers for both hosts on the private network. Rendering produces `zones/tailnet.zone` using the nodes' `Tailscale` addresses. Keep those live node addresses out of git. A `Tailscale` Service address is stable and differs from a node address.

Each zone file and its template, where one exists, carry their own serial.

When a record changes, raise the serial in the zone you change, updating both its `.tmpl` source and rendered `.zone` file where that pair exists. For the s3 views, update `zones/tailnet.zone.tmpl` and its rendered `zones/tailnet.zone` together when changing the `tailnet` zone. Update `zones/lan.zone` when changing the LAN zone. CoreDNS rereads a zone when its serial changes.

`ca.example.invalid`, `otlp.example.invalid` and `grafana.example.invalid` answer `Tailscale` clients and return REFUSED for other sources. Each zone has apex and `ns` A records: `zones/ca.example.invalid.zone` in the companion uses the stable `Tailscale` Service address `192.0.2.44`. The templates `zones/otlp.example.invalid.zone.tmpl` in the companion and `zones/grafana.example.invalid.zone.tmpl` in the companion use `@O11Y_TAILNET_IPV4@`, rendered as the host-c-o11y node address at deploy.

Neither s3 view has IPv6 records, so `AAAA` queries for the two s3 names should get an empty successful response in both views. For `ca.example.invalid`, `otlp.example.invalid` and `grafana.example.invalid` and their `ns` names, the `file` plugin in the `tailnet` view should return an empty successful answer to `AAAA` queries. Other sources should get REFUSED.

Compose mounts the `config/` directory at `/etc/coredns` and the `zones/` directory at `/zones`, both with `:ro`. Mounting the directory lets the container see a replacement `Corefile`. Query logs go to standard output and the Docker `json-file` driver, limited to three files of 10 MB each. Container settings include `read_only: true` for the root file system, a `256m` memory limit and `NET_BIND_SERVICE` as the capability.

## Ports and directions

Host networking preserves the source address of each query. CoreDNS uses `UDP` and TCP port 53 on the addresses in `DNS_BIND_LAN` and `DNS_BIND_TAILNET`. The metrics listener binds to localhost port 9153.

The `.:53` block caches and forwards other names to Cloudflare's malware-blocking resolver at `https://security.cloudflare-dns.com` over DNS over HTTPS (DoH), with `tls_servername security.cloudflare-dns.com`. Its access control allows LAN and `Tailscale` sources and refuses others. Startup resolution of that upstream name and failure recovery on the deployed hosts remain not verified here.

In the companion's `ops/tailnet` directory, the policy grants members, console devices, agent hosts, lab nodes, and the o11y sidecar access to port 53 over `UDP` and TCP on both Network Attached Storage (NAS) hosts. For each query, `Corefile` also checks the source.

## Configuration and secrets

The project uses no 1Password secret reference. Host-specific configuration goes in `/srv/example-storage/docker/dns/.env` with mode 0600 and in the rendered `tailnet` zone. This README records the deployment directory. Its live contents remain not verified here.

| Variable | Meaning |
| --- | --- |
| `DNS_BIND_LAN` | The host's LAN address. Required. |
| `DNS_BIND_TAILNET` | The host's `Tailscale` address. Required. |
| `DNS_LAN_TAILNET_ADDRS` | The comma-separated `Tailscale` addresses of LAN servers, each in single quotes. May be empty. |

[`render_lan_tailnet_addrs`](../../packages/tbhbops/src/tbhbops/dns.py) validates addresses and returns the single-quoted list. Wrap its output in double quotes in `.env` so Compose preserves the inner single quotes:

```text
DNS_BIND_LAN=<the host's LAN address>
DNS_BIND_TAILNET=<the host's address on the private network>
DNS_LAN_TAILNET_ADDRS="'<LAN server address on the private network>'"
```

For an empty list, write `DNS_LAN_TAILNET_ADDRS=`. A bare address inside the `client_ip() in [...]` expression in `Corefile` fails to parse. Check `/usr/local/bin/docker-compose -p dns config` from the project directory to confirm that the rendered value keeps its single quotes.

## Deploy

Copy reviewed and merged `apps/dns` files to `/srv/example-storage/docker/dns` on each host.

Render `zones/tailnet.zone` from `zones/tailnet.zone.tmpl`, replacing `@NAS-A_TAILNET_IPV4@` and `@NAS-B_TAILNET_IPV4@` with the nodes' `Tailscale` addresses. Render `zones/otlp.example.invalid.zone` and `zones/grafana.example.invalid.zone` from their templates, replacing `@O11Y_TAILNET_IPV4@` with the host-c-o11y node address. Read node addresses from the nodes themselves and keep them out of git. Copy `zones/ca.example.invalid.zone` unchanged. Its Service address is stable.

Write the three environment variables to `.env`.

Set directories to mode 0755 and `config/Corefile` and the zone files to mode 0644 after rendering. The container drops the capability that overrides file permissions, so restrictive file modes prevent it from reading its configuration.

From `/srv/example-storage/docker/dns`, check with `/usr/local/bin/docker-compose -p dns config`, then run `/usr/local/bin/docker-compose -p dns up -d`. Use the standalone binary's full path when the host's SSH environment omits `/usr/local/bin`. Run the checks against each resolver.

## Checks

The CI `dns-smoke` job runs on `apps/dns` changes and calls `uv run --locked tbhbops dns smoke --source apps/dns --artifacts <artifact-directory>`. [`dns_smoke.py`](../../packages/tbhbops/src/tbhbops/dns_smoke.py) uses isolated Docker bridge clients to check the source views and refusals. `test_dns_project.py` in the companion checks the Compose settings, `Corefile`, and zone shapes offline.

Query each of `ca.example.invalid`, `otlp.example.invalid` and `grafana.example.invalid` and its `ns` name through both resolvers over `UDP` and TCP. Clients on the private network should get `192.0.2.44` for `ca.example.invalid` and the host-c-o11y node address for `otlp.example.invalid` and `grafana.example.invalid`. LAN clients and other sources should get REFUSED for all three zones. The isolated Docker smoke checks these answers and refusals with fixture node addresses.

From an operator Mac on the private network, set `NAS` to the `Tailscale` address of each resolver and run:

```text
dig @"$NAS" nas-a.example.invalid A
dig @"$NAS" example.com A
dig +tcp @"$NAS" nas-a.example.invalid A
```

The first and third answers should use the `tailnet` view. The second should come through the forwarder. On the host, `/usr/local/bin/docker-compose -p dns logs coredns` should show the Mac's `Tailscale` address for both `UDP` and TCP queries. From host-a, `dig @"$NAS" nas-a.example.invalid A +short` should print the LAN answer, 198.51.100.41.

From a source outside the allowed ranges, set `NAS` to the LAN address of the resolver and check refusal:

```text
dig @"$NAS" nas-a.example.invalid A
dig @"$NAS" example.com A
```

Both queries should return status REFUSED if they reach CoreDNS. Live refusal from such a source remains not verified here. The smoke job checks refusal in isolation.

## Rollback and resolver fallback

To stop one resolver, run `/usr/local/bin/docker-compose -p dns down` from `/srv/example-storage/docker/dns`. Keep the project directory, `.env`, and rendered zone. To undo an upgrade, stage the previous merged image digest, run `up -d` and repeat the dig checks before changing the other host.

Clients have a resolver on nas-a and another on nas-b for redundancy. Coordination records that clients receive both through Dynamic Host Configuration Protocol (DHCP). The router sets their order outside this repository, not verified here. Client fallback behavior remains not verified here.

## Upgrades

Renovate bumps the CoreDNS image digest in Compose, and `dns-smoke` exercises that image for `apps/dns` changes. After review and merge of the bump, deploy one host, check a canary with the dig commands in Checks, then deploy the other host. This order is the operator practice recorded by coordination, not an automated procedure in this repository, and remains not verified here. If a canary fails, restore the previous digest on that host and repeat the checks.
