# 2. Constraints

| Constraint | Effect |
| --- | --- |
| Read-only GitHub App | The operator plans a read-only App with every read-only permission GitHub offers for repositories and the organization, and two PR event subscriptions. It never writes. This repo can't confirm the live App |
| REST only | The design reads GitHub through REST with conditional requests and doesn't use GraphQL |
| Outbound-only network | The operator plans for a Cloudflare Tunnel connector to route only `POST /webhook` to the service, and for the host to forward no port. The repo leaves route verification to ops |
| Single deployment | Production only, with no staging copy |
| Container image | A distroless static non-root image with one Go binary, a read-only root filesystem, and, in the design, one writable volume that `deploy/compose.yaml` doesn't have yet |
| Existing observability stack | The design sends OpenTelemetry data over HTTP to the host collector, which forwards traces, metrics, and logs to the backends. The code exports nothing yet |
| No tail sampling | A PR's trace remains incomplete for weeks, and the collector pipeline for this service must not tail-sample |
| Secrets as files | The webhook secret, the tunnel token, and the App private key use read-only file mounts. None is ever an environment variable or image content |
