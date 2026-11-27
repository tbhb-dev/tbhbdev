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
Configure the tunnel connector separately. Expose `POST /webhook` and leave the other routes private.

## Health and restarts

Run the installed binary with `-healthcheck` and the same `LISTEN_ADDR` to probe liveness. With the default address, the probe sends a `GET` request to <http://127.0.0.1:8080/healthz>. It exits with status 0 on success or 1 on failure. The planned readiness endpoint at <http://127.0.0.1:8080/readyz> (using the default listen address) would report readiness after the store opens and reconciliation finishes.

On `SIGTERM`, the service stops accepting requests and waits for the worker and reconciler. It then flushes metrics and closes the store.

After a rollback to a version that records metrics without durable claims, set `RECONCILE_FLOOR_RESET=1` for the first start after reinstalling a version that uses claims. Clear the flag to keep the stored repair floor on later starts.
