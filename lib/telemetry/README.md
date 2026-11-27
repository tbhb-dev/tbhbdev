# Telemetry

The Python library records one bounded trace for a synchronous command and
exports it over HTTP using OpenTelemetry.

Import [Agents (live)](dashboards/agents-live.json) in Grafana with a Tempo
data source `tempo`. The dashboard shows agent launches, outcomes,
and durations.
