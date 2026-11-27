# 8. Cross-cutting concepts

> Today versus design: the code has a webhook receiver and a GitHub client behind one request budget. The worker, the reconciler, the store, and the export remain design. These concepts describe the design for the rest.

## Identity

Trace IDs are the first 16 bytes of a 256-bit Secure Hash Algorithm (`SHA-256`) digest over a fixed prefix and the PR's GraphQL node ID. Span IDs are the first 8 bytes of a digest that also covers the span kind and a key unique within the PR. Webhook and reconciliation runs derive the same IDs.

## Request budget

One token bucket serves all callers, refilled at 2,500 requests an hour with a burst of 60 and 250 ms between requests. Callers wait by priority, from webhook to backfill. The bucket stops at a floor of 500 remaining requests until the limit resets. Steady state uses about 9% of GitHub's 5,000 request floor.

## Rate-limit handling

A `429`, or a `403` that cites a secondary limit, backs the budget off from one minute, doubling to 15 minutes. A `401` renews the installation token once. A redirect, a body over 16 MiB, or a target on another host is an error.

## Security

A request must have a valid signature to read or write state. Tokens come from the `TokenSource` interface in the [GitHub client](../../internal/ghclient), and `main` builds no client yet. The original design has the service sign the installation JSON Web Token with the App private key, mounted as a file without write access. No record covers another source of tokens. Secrets are files.

## Telemetry content

Spans hold repository and branch names, PR numbers, logins, team slugs, states, and times. The base metric attributes are the repository name and author type. The review counter adds the review state. The cycle time histogram adds the end state and start source. Titles, bodies, comment text, and diffs never appear.

## Logging

Today `main` logs JSON to standard output. The design adds a second handler that exports each record to the collector as an `OTLP` log, with `deployment.environment.name` set. Loki should index it as a label. Page 11's export spike checks that. In the design, each log line has the delivery ID, the event, and the result, and no body, credential, or signature header. The `HMAC` covers the body and excludes the headers. A replayed valid body can arrive with any header text. Today the receiver requires fixed patterns before it logs `X-GitHub-Delivery` and `X-GitHub-Event`: 1 to 64 letters, digits, or hyphens for the delivery ID, and 1 to 64 lowercase letters, digits, or `_` for the event. Anything else gets `400` and a log line with a fixed reason. In the design, logs use the SDK's batch log processor and keep nothing in the store. A collector outage drops the records the exporter can't send, and standard output still has them (source: tbhb-dev/wiki#56).

## Testing

Budget tests use Go’s [concurrency testing package](https://pkg.go.dev/testing/synctest) with a fake clock. The client tests use a fake `RoundTripper` because the clock can’t advance while a goroutine waits on a socket.
