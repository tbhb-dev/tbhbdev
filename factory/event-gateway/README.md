# Event gateway

<!-- cspell:words healthz -->

Webhooks from services such as GitHub and Grafana go into a durable JetStream log.
Temporal workflows and agent sessions receive events from this log.
Build the gateway with `bazelisk build //factory/event-gateway/cmd/event-gateway`.
Run its tests with `bazelisk test --config=ci //factory/event-gateway/...`.

See the [Go module](go.mod) for dependencies and the [gateway design](https://github.com/tbhb-dev/wiki/tree/main/designs/event-gateway) for architecture.
The service is pre-alpha.

## Container and deployment

CI builds the image for Linux amd64 and arm64 and checks the image's entrypoint and data directory permissions.
Build the image index with `bazelisk build -c opt //factory/event-gateway/container:image_multiarch`.
Load the local image into Docker with `bazelisk run -c opt //factory/event-gateway/container:image_load`.

The image runs as user and group 65532. Compose keeps the webhook listener on the internal network and publishes the session listener on the configured private IPv4 address.
See the [Ansible role](../../ops/ansible/roles/event_gateway_deploy/README.md) for deployment steps.

## Health check

`event-gateway healthcheck [-addr HOST:PORT]` requests `GET /healthz` (default `127.0.0.1:8080`, 4 second timeout) and exits 0 for HTTP 200.
It prints nothing on success. Other responses and request failures exit 1.

## Read API

Enable the read API with `event-gateway serve -read-listen ADDRESS -read-token-file FILE`.
Bind this separate listener to a private address and keep it off public ingress.
The file contains one `name:sha256hex(token)` entry per line.
When reading the file, the gateway skips blank lines and comments that begin with `#`. It checks for changes every second and keeps the previous token set if a reload fails.
Send `Authorization: Bearer <token>` with each request.

- `GET /v1/events?after=N` returns up to 10 events in ascending stream sequence, strictly after `N` (default 0).
  Use the response's `last_seq` as your next `after` value.
  The response contains an `events` array and, when retention removed older events, a `gap` object with inclusive `from` and `to` sequences.
  For an empty page, `last_seq` is the gap's end or the supplied cursor if there's no gap.
  Optional `provider` and `event` query parameters match exact field values.
- `GET /v1/events/{seq}` returns the stored envelope with its sequence and payload.
  It returns 410 for an expired sequence, 404 for a missing sequence, and 400 for an invalid sequence.

Licensed under the [Apache License 2.0](../../LICENSE).
