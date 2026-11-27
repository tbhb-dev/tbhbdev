# 7. Deployment view

Build the service binary from the monorepo root. The host operator manages
the process and the tunnel connector.

```sh
bazelisk build //factory/github-metrics/cmd/github-metrics
```

Install the binary reported by Bazel on the deployment host. Run it under a
non-root account with a persistent, writable directory for its store.
Configure the host's process supervisor to restart the service on failure
and collect its JSON logs from standard output.

## Service configuration

| Variable | Setting |
| --- | --- |
| `WEBHOOK_SECRET_FILE` | Path to a non-empty file containing the GitHub App webhook secret |
| `STORE_FILE` | Path to the persistent store for the webhook queue and reconciliation state |
| `LISTEN_ADDR` | Listen address, defaulting to `:8080`. Bind to an address reachable by the tunnel connector |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The host collector's HTTP endpoint for metric export. Required for reconciliation |
| `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_APP_KEY_FILE` | Set all three for reconciliation. Use positive integer IDs and a path to the App private key file |
| `GITHUB_API_URL` | HTTPS base URL for the GitHub API, defaulting to `https://api.github.com` |

Give the service account read access to the webhook secret and App key
files. Keep the file contents out of environment variables and logs.
Configure the tunnel connector separately and expose only `POST /webhook`
over the tunnel.

## Health and restarts

Run the installed binary with `-healthcheck` and the same `LISTEN_ADDR` to
probe the local `GET /healthz` endpoint. The probe exits with status 0 on
success and 1 on failure. The planned `/readyz` endpoint would report
readiness after the store opens and reconciliation finishes.

Send `SIGTERM` for an orderly shutdown. The service stops accepting
requests, waits for the worker and reconciler, flushes metrics, and closes
the store.

If you roll back to a version that records metrics without durable claims,
set `RECONCILE_FLOOR_RESET=1` for the first start after reinstalling a version
that uses claims. Clear it for subsequent starts so they retain the stored
repair floor.
