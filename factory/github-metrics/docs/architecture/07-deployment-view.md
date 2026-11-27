# 7. Deployment view

The following notes describe the deployment at the frozen source commit.
The monorepo builds the service binary. The container build and deployment
tools documented here stayed with the source repository.

The target deployment runs two containers on the production container host: the service and the tunnel connector. Today `deploy/compose.yaml` mounts the webhook secret, the App private key, and the tunnel token. A volume stores the queue, while the receiver doesn't read `GITHUB_APP_KEY_FILE` yet. The table describes the target.

| Element | Setting |
| --- | --- |
| Image | Distroless static, non-root, one Go binary, referenced by digest |
| Filesystem | Read-only root, one writable volume at `/var/lib/github-metrics` |
| Network | Off the host network. The tunnel connector makes only outbound connections |
| Secrets | Read-only files. The service gets the webhook secret and the App private key. The connector gets the tunnel token |
| Configuration | Environment variables set the App ID, the installation ID, the listen addresses, the schedule, and the collector endpoint |
| Health | `/healthz` for liveness. `/readyz` is a design item that reports an open store and one finished reconciliation |
| Shutdown | On `SIGTERM`, within 20 seconds the receiver stops accepting requests and the worker finishes its current PR. Then the meter provider flushes and the store closes |
| Delivery | The repository publishes the image and a Compose file. The host operator deploys them |

The host log collector reads Docker's `json-file` logs and labels them by Compose project and service. The receiver and tunnel use this driver and rotate their logs at 10 MB, keeping up to three files per container.

## Secret files

Keep secret contents out of images, environment variables, and logs. Git ignores `deploy/secrets/`.

The webhook secret and tunnel token need user 65532 and mode 400. A separate process writes `github_app_key` with the deploy account as owner and mode 600. Start deployments with `deploy/up.sh`. The script rejects a missing key, a symlink source, and source modes other than 400 or 600. A pinned BusyBox helper uses Docker access to create `secrets/runtime/` with root ownership and mode 700. The copy gets mode 400 and owner 65532:65532 before a rename publishes it. Host users can't traverse that directory, including user 65532. The Docker daemon mounts the file at `/run/secrets/github_app_key`, where the receiver's user can read it. Compose sets `GITHUB_APP_KEY_FILE` to that path.

The helper doesn't use the network or write container logs. It never prints the key.

A failed check or copy stops deployment before Compose can start the services. Each launch recreates the containers to replace the receiver's old file mount after key rotation. Keep the runtime copy for container restarts and remove it through Docker when retiring the deployment. Use a local Docker daemon without user namespace remapping to preserve numeric host ownership.

For an account with only `sudo docker` access, run `DEPLOY_DOCKER_SUDO=1 deploy/up.sh` with the image argument. It doesn't need sudo for file tools. Put `METRICS_NETWORK` and optional Compose settings in `.env` beside `compose.yaml` when sudo filters the environment. The script supplies the image reference and resolver path through a temporary Compose environment file and removes it on exit.

`RECEIVER_RESOLV_CONF` is required. Set it to the path of an existing, non-empty regular file on the deployment host that lists the upstream resolvers. Export the variable or set it to a literal path in `.env` beside `compose.yaml`. Relative paths start from that directory. `deploy/up.sh` checks the file before it calls Docker.

Mounting this file in the receiver replaces Docker's embedded DNS for that container. Set the receiver's collector endpoint to an IP address so the receiver can reach the collector.

With `GITHUB_METRICS_IMAGE` and `METRICS_NETWORK` also set, run `docker compose -f deploy/compose.yaml config` to review the mount and environment settings without reading the key. Check that only the receiver mounts `github_app_key`, that its source ends in `secrets/runtime/github_app_key`, and that `GITHUB_APP_KEY_FILE` names `/run/secrets/github_app_key`. This command checks configuration, not file access. Run the helper on the deployment host before starting the containers.
