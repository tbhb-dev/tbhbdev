# event-gateway

The event gateway receives webhook events from sources such as GitHub and Grafana, keeps a durable log of them, and delivers them to workflows and agent sessions.

The gateway uses NATS JetStream to store events and can deliver to Temporal workflows.
Build it with `bazel build //factory/event-gateway/cmd/event-gateway`.
Run its tests with `bazel test --config=ci //factory/event-gateway/...`.

Status: pre-alpha.

The design is in tbhb-dev/wiki under [designs/event-gateway](https://github.com/tbhb-dev/wiki/tree/main/designs/event-gateway).

## Healthcheck

`event-gateway healthcheck [-addr HOST:PORT]` requests `GET /healthz` (default `127.0.0.1:8080`, 4 second timeout) and exits 0 only on HTTP 200.
It prints nothing on success and exits 1 otherwise.

## Read API

Enable the read API with `event-gateway serve -read-listen ADDRESS -read-token-file FILE`.
Bind this separate listener to a private address and keep it off public ingress.
The file contains one `name:sha256hex(token)` entry per line.
The gateway skips blank lines and lines beginning with `#`, checks the file every second, and keeps the previous set if a reload fails.
Send `Authorization: Bearer <token>` with each request.

- `GET /v1/events?after=N` returns up to 10 events in ascending stream sequence, strictly after `N` (default 0).
  Use the response's `last_seq` as your next `after` value.
  The response contains an `events` array and, when retention removed older events, a `gap` object with inclusive `from` and `to` sequences.
  For an empty page, `last_seq` is the gap's end or the supplied cursor if there's no gap.
  Optional `provider` and `event` query parameters match those fields exactly.
- `GET /v1/events/{seq}` returns the stored envelope with its sequence and payload.
  It returns 410 for an expired sequence, 404 for a missing sequence, and 400 for an invalid sequence.

Licensed under the [Apache License 2.0](../../LICENSE).
