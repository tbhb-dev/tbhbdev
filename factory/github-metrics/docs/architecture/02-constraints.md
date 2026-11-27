# 2. Constraints

| Constraint | Effect |
| --- | --- |
| GitHub App permissions | The operator plans an App with every read permission GitHub offers for repositories and the organization, zero write permissions, and two PR event subscriptions. This repo can't confirm the live App |
| REST | The design reads GitHub through REST with conditional requests and doesn't use GraphQL |
| Outbound network | The operator plans for a Cloudflare Tunnel connector to route `POST /webhook` as its one public endpoint. The host forwards no ports. The repo leaves route verification to ops |
| Single deployment | One production deployment and no staging copy |
| Container image | The deployment design calls for a non-root image with one static Go binary, no Linux distribution tools, a root filesystem mounted without write access, and one writable volume for the store. See the [deployment view](07-deployment-view.md) for the binary and its configuration |
| Existing observability stack | The design sends OpenTelemetry data over HTTP to the host collector, which forwards traces, metrics, and logs to the backends. The code exports nothing yet |
| No tail sampling | A PR's trace remains incomplete for weeks, and the collector pipeline for this service must not tail-sample |
| Secrets as files | File mounts hold the webhook secret, the tunnel token, and the App private key without allowing writes. None is ever an environment variable or image content |
