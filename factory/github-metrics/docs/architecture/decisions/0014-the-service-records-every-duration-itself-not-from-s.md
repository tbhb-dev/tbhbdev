# D14. The service records every duration itself, not from spans

- **Status:** accepted, 2026-11-14.
- **Rule:** the service records each timing, size, count, and gauge as a direct OTel instrument, with the commit-then-record rule of D10. The service gets none of these instruments from the metrics generator in Tempo. Alloy span metrics are an optional extra and no goal or alert reads them. The generator in Tempo v3.1.0 drops spans that ended more than 30 seconds before ingestion (`MetricsIngestionSlack`, default 30 s, in `modules/generator/config.go`), and this service backdates its spans to GitHub's timestamps. The deployment runs Tempo 3.1.0 and keeps the default slack of 30 s.
- **Undo:** derive series from spans after raising `MetricsIngestionSlack` past the longest backdating, and accept double counts on a reexport.
