---
type: Readme
title: Event gateway deployment
description: Deploy the pinned event gateway and its outbound tunnel on host-c.
tags: [ansible, event-gateway]
status: draft
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: medium
    surface: exec
    session: sha-256-64:a70c9a1a38534d54
    at: "2026-11-25T15:18:30Z"
---

The `ansible/playbooks/event-gateway.yml` playbook targets `host-c` through the `event_gateway` inventory group. It checks out the pinned commit as `ops` and builds the gateway image locally when its commit tag is missing. The deployment script receives the Docker image ID and uses `--pull never`. Before applying, pull the pinned tunnel image.

## Deployment prerequisites

From the administrator VM, provision the Cloudflare route and both secret files in `/home/ops/event-gateway-deploy/repo/deploy/secrets/`. Install Docker and Compose, and allow `ops` to run Docker with `sudo`. On a fresh checkout, the role stops at the missing paths so the administrator VM can provision them before the next run.

## Rollback

To undo the deployment, run these commands from the checkout. The `-v` flag removes the gateway's event store.

```sh
sudo env EVENT_GATEWAY_IMAGE=unused docker compose -f deploy/compose.yaml down -v
sudo docker rmi event-gateway:f31b3ec2
```
