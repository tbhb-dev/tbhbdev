---
type: Readme
title: The observability stack role
description: Stage an observability release on its VM and recover from a failed health gate.
tags: [ansible, o11y, ops]
status: draft
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:181534a2377b78aa
    at: "2026-11-24T10:56:31Z"
---

<!-- cspell:words tbhbops -->

`o11y` stages an observability release and checks readiness after startup. Use `ansible/playbooks/o11y.yml` to deploy or `ansible/playbooks/o11y-rollback.yml` to roll back. Both playbooks target `o11y` with root privileges and gathered facts. They run one host at a time. Get the operator's approval before either host operation.

Install Docker and Compose first. Preflight checks the free space in `ansible_mounts` against the configured reserve. Both SSH argument variables must exclude `lab-ops`. Use the VM inventory's controller credentials and known host keys because this role doesn't install SSH keys.

## Required inputs

Set these variables explicitly. Deployment supplies no defaults for them.

| Variable prefix `o11y_` | Meaning |
| --- | --- |
| `root` | Absolute deployment root, without a trailing slash |
| `data_dir` | Absolute persistent data directory outside releases |
| `release_id` | Full 40 character ops commit, available in the controller checkout |
| `host_name` | Private VM name recorded on telemetry |
| `grafana_root_url` | `Grafana` URL for eventual access over `Tailscale` |
| `grafana_owner_login` | Owner's email login |
| `alert_email_to`, `alert_smtp_from` | Alert recipient and sender email addresses |
| `alert_smtp_host`, `alert_smtp_port`, `alert_smtp_user` | Simple Mail Transfer Protocol (SMTP) connection settings with Transport Layer Security (TLS) verification enabled |
| `alert_discord_webhook_ref`, `alert_smtp_password_ref` | Two `vault / item / field` references |
| `secrets_vault` | Vault name that both references must use |
| `lookup_command` | Controller command invoked with one shell-quoted reference |

Set `o11y_lookup_command` to `env -u SSH_AUTH_SOCK tbhbdev secret read`, following `o11y_vm_tailscale_lookup_command`. The controller resolves each reference directly into `copy` content. The role never stores resolved values in facts or registered variables. Secret tasks use `no_log: true` and `diff: false`. Both alert files belong to user and group IDs 472 with mode `0400`.

The role includes `tbhbops_tool` for the existing `ops` account, pinned to `release_id`. The controller needs git, `uv`, and `tbhbops` on `PATH`. It archives the stack from that commit and runs `tbhbops o11y render` in a temporary directory, with the virtual machine's final configuration paths. Cleanup removes controller scratch files after the render or a failure. On the VM, `tbhbops o11y init` creates the data layout, RustFS keys, and `Grafana` administrator password. Those generated values stay on the VM. Alert values arrive afterward from the controller reads.

## Defaults

Default values cover disk reserve, health retries, and optional network inputs.

| Variable prefix `o11y_` | Default |
| --- | --- |
| `min_free_gib` | 50 |
| `health_retries`, `health_delay` | 30 attempts, 5 seconds between attempts |
| `ts_hostname` | Empty. This leaves the `Tailscale` sidecar off. |
| `fixed_addresses` | `"false"`. Set to `"true"` for the live container address map |
| `grafana_tls_cert_dir` | Empty. This leaves the `Grafana` TLS proxy off. |

### Optional network settings

Set `ts_hostname` to one host label to enable the user-space sidecar and its outbound HTTP proxy on container port 1055. State persists under `<data_dir>/ts`. If that directory is empty, the operator must authenticate on first start. This role supplies no auth key. The fixed address map is independent of the sidecar. During rendering, the tool reads `O11Y_TS_HOSTNAME` and `O11Y_FIXED_ADDRESSES`.

Compose uses the port mappings in `o11y/compose.yaml`. These are `Grafana` 3000 and OpenTelemetry Protocol (OTLP) 4317/4318 on the local host interface. It doesn't publish extra ports. Access over `Tailscale` comes later.

For the TLS proxy, set `grafana_tls_cert_dir` (`O11Y_GRAFANA_TLS_CERT_DIR` for the renderer) to an absolute host directory containing `grafana.crt.pem` and `grafana.key.pem`. The proxy mounts the directory at `/tls` with `:ro`. Provision and renew certificates on the host. The optional `nginx` proxy listens on container port 8443 without publishing a host port. With fixed addresses enabled, it binds to `203.0.113.9` and forwards to `Grafana` at `203.0.113.7`. Otherwise it resolves the destination through container DNS. The certificate path accepts letters, digits, `_`, and `-` in nonempty path components. Disabling the input removes both the service and its rendered configuration.

## Lock

An atomic `mkdir <root>/deploy.lock` claims the deployment lock. Contenders fail with the full lock path. Cleanup in `always` removes the lock if this run claimed it. The lock has no owner metadata. After a killed process leaves one behind, check that no run is active and remove it by hand:

```console
rmdir <root>/deploy.lock
```

## Releases

The role stores each release at `<root>/releases/<release_id>`. Persistent data and secrets stay outside releases. A generated `.env` supplies the release settings. Staging validates `docker compose config -q` and pulls images before changing `current`. An input digest rejects configuration changes under an existing release ID, protecting the running release. Use a new ops commit for changed release configuration.

Staging leaves `current` and running services alone.

## Activation and recovery

Before activation, the role records the old `current` target as `previous`. Startup runs `docker compose up -d --wait`, then reads `docker compose ps --all --format json` in the selected release. For `Grafana`, RustFS, Loki, Tempo, Mimir, and Alloy, the gate requires one running container per service. Containers with Compose health checks must report `healthy`. Initialization jobs aren't part of this gate. For a container without a health check, the role retries its readiness probe inside the container through `docker compose exec -T`.

On failure, rescue stops the candidate before starting the old release with the same health gate. Successful recovery restores `current`. Rescue always fails the play, even after successful recovery. A failed first deployment leaves the stack down and `current` unset. Recovery or shutdown errors identify services to inspect, leaving `current` unset when recovery fails. Data migrations and shared secrets aren't rolled back.

## Rollback

The rollback playbook selects `previous` and uses the same transaction. Set `root` and any reserve or retry overrides. Rollback runs without secret reads or a new release ID, and records the replaced release as `previous`.

## Verification

Python tests check required argument specifications, default parity, rendered environment paths, the absence of extra port bindings, and the transaction structure.

Molecule's Docker scenario uses a privileged Ubuntu container with a nested Docker daemon. It installs the real pinned tool and runs real render and init commands. Six small HTTP services replace the component images. The controller render wrapper replaces the rendered Compose file and preserves the other files. Production task files stay in use. Converge and a repeat apply run the deployment twice with diffs enabled.

Verify starts a second release with every container reporting `healthy`, then makes `Grafana` fail its health check. The container health gate must reject that release and then the deployment checks recovery. It requires `current` to return to the first release with healthy running services. It then deploys a third release without a `Grafana` health check that passes its container exec probe and imports the rollback playbook to restore the first release. A failed first deployment also exercises exec probe failure. Checks of container publications allow `Grafana` 3000 and OTLP 4317/4318 on the local host interface and reject any other publication. A leftover lock must refuse deployment and remain after that attempt. Checks cover registered variables and the captured converge log, including diffs. Assertions reject either synthetic secret canary. The fixture supplies both values locally.

```console
uv run --project ansible ansible-galaxy collection install -r ansible/requirements.yml -p ansible/collections
export ANSIBLE_COLLECTIONS_PATH="$PWD/ansible/collections"
export ANSIBLE_ROLES_PATH="$PWD/ansible/roles"
cd ansible/roles/o11y
uv run --project ../.. molecule test -s default
```

Try `molecule syntax -s default` in the same environment when Docker is unavailable. Some Docker driver versions require a daemon even for syntax. Use `ansible-playbook --syntax-check` to check the scenario playbooks when the driver requires Docker, though it can't prove runtime recovery or that a repeat apply leaves the deployment unchanged.
