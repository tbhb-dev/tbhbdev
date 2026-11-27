# D9. Export over HTTP with protobuf to the host's collector

- **Status:** accepted, 2026-11-14.
- **Rule:** the service exports OpenTelemetry Protocol (OTLP) data over HTTP to port 4318 and exports trace spans synchronously per reconcile, without the batch processor. Metrics leave through a periodic reader every 60 seconds. Logs leave over the same endpoint through the OTLP log exporter, with `deployment.environment.name` set, and the original design says the collector sends them to Loki and that every service does so. The repository builds none of this export yet, and the export spike on page 11 is the first check of the log path. Logs use the SDK's batch log processor and keep nothing in the store. A collector outage drops the records the exporter can't send, and standard output still has them (source: tbhb-dev/wiki#56).
- **Undo:** switch the exporter to gRPC on port 4317, which changes the configuration and the dependencies.
