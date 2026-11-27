# 12. Glossary

| Term | Meaning |
| --- | --- |
| arc42 | A template for architecture documentation |
| C4 | A way to draw software at four levels: context, container, component, and code |
| `bbolt` | An embedded key-value store that keeps its data in one file |
| Backfill | The first-install walk over every repository's PR history. It produces traces and no metrics |
| Delivery | One webhook request from GitHub |
| `ETag` | A response tag. A later request sends it to check whether anything changed |
| `HMAC` | A keyed hash that GitHub uses to sign each delivery body |
| `OTLP` | The OpenTelemetry Protocol, the wire format for traces, metrics, and logs |
| Reconcile | Fetch a PR's current state from REST and export what's new |
| Watermark | The newest update time a repository's incremental run has seen |
