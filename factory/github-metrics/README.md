# github-metrics

A Go service that turns GitHub pull request activity into OpenTelemetry
traces and metrics.

See the [architecture](docs/architecture/README.md), the
[reconciler backfill design](docs/design/reconciler-backfill.md), and the
[repository instructions](../../AGENTS.md).

Build the service and run its tests from the repository root.

```sh
bazelisk build //factory/github-metrics/cmd/github-metrics
bazelisk test --config=ci //factory/github-metrics/...
```

Licensed under Apache-2.0.
