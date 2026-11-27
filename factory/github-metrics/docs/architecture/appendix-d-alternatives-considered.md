# Appendix D: alternatives considered and rejected

Today the implemented pieces are the webhook receiver, the request budget, and the GitHub client. Everything else is design or planned. Still to build are the worker, the reconciler, the store, and the OTel export. Treat every statement about them as a plan.

Each section below covers one option the design didn't take, and why. A section links the decision record that an option would undo. Appendix B of the original design, in the `drafts/github-metrics` folder of `tbhb-dev/wiki` at commit `8e06fb1`, is the source.

| Option not taken | Decision record |
| --- | --- |
| [A stateless service](#a-stateless-service) | [D4](decisions/0004-the-service-stores-its-state-in-a-small-bbolt-fi.md) |
| [SQLite instead of `bbolt`](#sqlite-instead-of-bbolt) | [D4](decisions/0004-the-service-stores-its-state-in-a-small-bbolt-fi.md) |
| [One complete trace at merge or close](#one-complete-trace-at-merge-or-close) | [D5](decisions/0005-child-spans-export-when-complete-and-the-root-ex.md) |
| [Random IDs with a lookup table](#random-ids-with-a-lookup-table) | [D3](decisions/0003-trace-and-span-ids-derive-from-the-pr-s-graphql.md) |
| [One query per pull request](#one-query-per-pull-request) | [D8](decisions/0008-the-service-reads-github-through-rest-with-condi.md) |
| [A streaming export protocol](#a-streaming-export-protocol) | [D9](decisions/0009-export-over-http-with-protobuf-to-the-host-s-col.md) |
| [The collector's own GitHub receiver](#the-collectors-own-github-receiver) | None |
| [The redelivery API as a healer](#the-redelivery-api-as-a-healer) | [D7](decisions/0007-reconciliation-is-the-only-healer.md) |
| [Author login as a metric attribute](#author-login-as-a-metric-attribute) | [D10](decisions/0010-repository-and-author-type-are-the-only-metric-a.md) |
| [Re-exporting the root after a reopen](#re-exporting-the-root-after-a-reopen) | None |
| [Webhook payloads as a telemetry source](#webhook-payloads-as-a-telemetry-source) | None |

## A stateless service

The design could have the service re-derive everything from GitHub on each pass and not store anything between passes. The design can't rely on OpenTelemetry (OTel) backends to drop a span exported twice, and a histogram counts every observation. Each pass would double the metrics. A mounted volume with one `bbolt` file needs a small number of megabytes and keeps the root filesystem mounted without write access. [D4](decisions/0004-the-service-stores-its-state-in-a-small-bbolt-fi.md) records the stored-state choice.

## SQLite instead of `bbolt`

SQLite gives queries and a familiar format. A static binary with `CGO_ENABLED=0` needs a pure Go SQLite port, which adds a large dependency for what amounts to a small number of key-value buckets. `bbolt` is pure Go, small, and transactional, and an interface in front of the store allows a swap later. That swap is the undo path in [D4](decisions/0004-the-service-stores-its-state-in-a-small-bbolt-fi.md).

## One complete trace at merge or close

The design could wait until the pull request (PR) ends before it exports any span. Backends would receive whole traces, which suits tail sampling. That choice hides open PRs for weeks, though those are the ones a reader most wants to inspect, and it needs the store to keep each pending span. Incremental child spans with a late root show progress, at the cost of a missing root while the PR is open. Delaying every span until the terminal state is the undo path in [D5](decisions/0005-child-spans-export-when-complete-and-the-root-ex.md).

## Random IDs with a lookup table

Random trace and span IDs stored per PR would also give one trace per PR. The service then relies entirely on the store for IDs. Losing the file splits every open PR’s trace in two. Derived IDs come out the same after a lost store, and webhook and reconciliation runs agree without coordination. [D3](decisions/0003-trace-and-span-ids-derive-from-the-pr-s-graphql.md) records the derived-ID rule.

## One query per pull request

One GraphQL query can fetch a PR with its reviews, commits, and timeline, which saves requests. GraphQL has its own point-based limit and no conditional requests, and every poll costs points even when nothing changed. REST with `If-None-Match` makes the common idle poll free, and the original request asked for REST. Moving the per-PR fetch to one GraphQL query is the undo path in [D8](decisions/0008-the-service-reads-github-through-rest-with-condi.md).

## A streaming export protocol

The OpenTelemetry Protocol (OTLP) over gRPC streams well and is the collector's other default. At a rate in the low hundreds of spans per hour, streaming buys nothing, and gRPC adds a large dependency tree to the binary. Debugging HTTP with protobuf takes less tooling, and switching later is a configuration change. Switching to gRPC is the undo path in [D9](decisions/0009-export-over-http-with-protobuf-to-the-host-s-col.md).

## The collector's own GitHub receiver

The OTel Collector has a contributed GitHub receiver that scrapes repository metrics and can take webhooks. Its metrics are per-change gauges, not histograms per repository (unverified: the design doesn't cite a source for this). As far as the design knows, it builds no deterministic PR lifecycle trace, and that claim is unverified. A small dedicated service fits the trace model and the idempotency rules better.

## The redelivery API as a healer

The service could list failed deliveries and redeliver them with an App-level JSON Web Token (JWT). Reconciliation recovers what the REST API exposes. It can’t recover facts exclusive to webhook payloads, such as whether someone resolved a review thread. The design doesn't record such a fact. Reconciliation restores everything the design does record, which suffices. Redelivery would add lower latency and nothing more, at the cost of more code and a second credential use. Adding a redelivery client is the undo path in [D7](decisions/0007-reconciliation-is-the-only-healer.md).

## Author login as a metric attribute

Per-author histograms would show who waits longest. Public repositories accept PRs from anyone, so the login set has no bound, and per-person timing invites a use nobody asked for. Traces still record the author of each PR. [D10](decisions/0010-repository-and-author-type-are-the-only-metric-a.md) lists the metric attributes the design keeps.

## Re-exporting the root after a reopen

The root could go out again with the same IDs and a later end time each time a reopened PR changes state. That relies on how each backend treats two spans with one ID, which the design hasn't checked. The stateless option earlier in this appendix makes the same point: the design can't rely on backends dropping a repeat. Child spans for the reopen and the later close or merge record the same facts without a duplicate root.

## Webhook payloads as a telemetry source

The worker could read facts such as review thread resolution straight from webhook bodies. A missed delivery would then lose those facts permanently, because reconciliation can't fetch them. Some changes also have no GitHub timestamp in the payload. Using REST for all telemetry content keeps one source of truth and lets the queue drop each body once the worker marks the PR.
