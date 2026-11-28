---
type: Readme
title: Event gateway deployment
description: Deploy the pinned event gateway and its outbound tunnel.
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

The `ansible/playbooks/event-gateway.yml` playbook targets the `event_gateway` inventory group.
Set `event_gateway_deploy_commit` to a full monorepo commit and `event_gateway_deploy_session_bind` to the private IPv4 address for the session listener.
The role checks out that commit as `ops`. When its image tag is missing, `bazelisk` builds the image and the role loads it into Docker.
The deployment script uses the local Docker image ID and passes `--pull never`.

## Deployment prerequisites

Install `bazelisk`, Docker, and Compose. Allow `ops` to run Docker with `sudo`.
Pull the tunnel image pinned in `factory/event-gateway/deploy/compose.yaml` and provision the Cloudflare route.
Under the checkout's `factory/event-gateway/deploy/secrets/`, provision `github_webhook_secret`, `event_gateway_session_tokens`, and `tunnel_token`.
Each must be a regular file owned by user ID 65532 with mode 0400.
The role reports missing files after checkout so the administrator can provision them before the next run.

## Rollback

To return to a previous imported version, set `event_gateway_deploy_commit` to its full monorepo commit and apply the playbook again.
