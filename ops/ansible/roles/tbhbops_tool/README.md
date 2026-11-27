---
type: Readme
title: Install the ops command-line tool
description: Install the ops command-line tool on Linux from wheels the controller builds at a pinned ops commit.
tags: [ansible, ops, tbhbops]
status: draft
generated:
  - role: generated
    harness: codex:0.166.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:e770bc7d77f07d1a
    at: "2026-11-22T00:01:07Z"
---

<!-- cspell:words tbhbops venv -->

`tbhbops_tool` gives an existing Linux account its own `tbhbops`. Set `tbhbops_tool_user` to that account. The role refuses the operator's account, `tony`. Gather facts and allow `Ansible` to become that user. `playbooks/lab-tbhbops.yml` applies it to the `agent` account on the `lab` hosts.

## Pinned source

`tbhbops_tool_commit` defaults to `86922cc7b48e2adf9e89ebac6d30014d38cf04d0`, the ops main commit that added `connect check --inventory` (tbhb-dev/tbhbdev#222). Use a full 40 character commit. Before running the role, fetch that commit into the controller's `tbhbops_tool_repo`. This path defaults to `{{ playbook_dir }}/../..` for plays in `ansible/playbooks`. A missing local commit fails with a fetch instruction.

Renovate can't bump `tbhbops_tool_commit`. A tool change needs a manual bump of that default or the caller's override.

An archive of the pinned tree supplies the build source without changing the checkout. Against that archive, `uv export --package tbhbops-cli --frozen --no-dev --no-emit-workspace --no-editable --format requirements-txt --no-header` exports exact third-party versions and hashes from `uv.lock`. Omitting the header keeps temporary paths out of the digest.

## Wheels and requirements

On the controller, `uv build --all-packages --wheel` builds the two workspace wheels. Each must exist before `Ansible` computes its sha256 digest. The final `requirements.txt` combines the export with these local wheel entries:

```text
tbhbops @ file:///<absolute host wheel path> --hash=sha256:<digest>
tbhbops-cli @ file:///<absolute host wheel path> --hash=sha256:<digest>
```

Wheel builds are byte-reproducible. The marker's requirements digest depends on this. Molecule's second converge reporting `changed=0` is the evidence.

`Ansible` copies both wheels and `requirements.txt` to `~/.cache/tbhbops-tool/<commit>/` on the host. Those absolute paths appear in the final requirements. Host-side `stat` checks each copied wheel against its controller digest, then checks the requirements digest. Source trees and GitHub credentials stay on the controller.

## Install layout

All paths belong to the tool account:

```text
~/.local/bin/uv                         pinned uv executable
~/.local/bin/tbhbops                    symlink to venv/bin/tbhbops
~/.local/share/tbhbops-tool/venv/        dedicated Python environment
~/.local/share/tbhbops-tool/python/      downloaded CPython
~/.local/share/tbhbops-tool/marker       commit= and requirements_sha256=
~/.cache/tbhbops-tool/<commit>/          wheels and hashed requirements
```

`Ansible` manages the command symlink with `file` and `state: link`.

## Installation

Recreate when the marker changes and reuse otherwise. After removing the old marker and virtual environment, `Ansible` runs `uv venv --python 3.15 ~/.local/share/tbhbops-tool/venv`. Both projects require Python 3.15 or later. If the host lacks it, `uv` downloads `CPython 3.15` under `~/.local/share/tbhbops-tool/python`. The pinned `uv` verifies it with built-in checksums, so the `uv` pin and its sha256 cover the interpreter. Next, `uv pip install --python <venv>/bin/python --require-hashes --no-deps --reinstall -r <cache>/requirements.txt` installs the complete exported list. Hash checking covers third-party dependencies and both workspace wheels. `--no-deps` prevents additions outside that list, while `--reinstall` repairs an unhealthy installation.

## Installed state

Before writing the marker, `Ansible` requires successful `~/.local/bin/tbhbops --help` and `~/.local/bin/tbhbops o11y --help`. With a matching marker and both checks passing, the host reports `changed=0`. Controller exports and builds still run because the expected final digest covers wheel hashes as well as host paths. A failed health check with a matching marker triggers a fresh installation into the existing virtual environment.

## Download checks

Defaults cite the published `.tar.gz.sha256` files from the [`uv` 0.13.8 release](https://github.com/astral-sh/`uv`/releases/tag/0.13.8). When installation needs a different `uv` version, `get_url` verifies the Linux `glibc` archive before extraction. Both x86_64 and aarch64 use `uv` 0.13.8, matching `mise.lock`. Package and Python downloads need working HTTPS.

## Check mode

Check mode leaves the host untouched. To compare the complete expected digest, the controller archives the commit and exports its lock, then builds and hashes both wheels. These tasks use `changed_when: false`. Cleanup always removes the scratch directory. A debug task reports changed when installation is pending, while a current host reports `changed=0`. Account reads, marker reads, and both help checks still run.

## Controller requirements

Provide git, tar, and `uv` on the controller's `PATH`, with the requested commit present locally. The host needs Python for `Ansible`, tar, and working HTTPS. Delegated controller tasks use `run_once` and `become: false`. The source and build blocks run once per batch and share what they register with every host in it, because the `include_tasks` that loads them runs on each host. Use one commit and controller checkout per play with the default linear strategy. After the batch finishes using the wheels, an `always` task removes the controller scratch directory once, including on failure.

## Verification scope

Molecule creates two Ubuntu 24.04 containers, each with an `ops` account, and runs them in one batch. The sequence checks an empty install in check mode before convergence. A repeat run must report `changed=0` for both hosts. Converge requires both hosts to share the same controller scratch path and checks its removal after the role. Verify runs on both hosts and requires the virtual environment interpreter's real path to stay under the tool account's home. It runs both help commands and compares the marker digest against the copied requirements. An installed PyYAML version check confirms its pin.

For the negative test, verify copies the requirements. It changes the hash for the wheel that contains the command-line tool and keeps the other hashes. A separate temporary virtual environment receives the attempted `uv pip install --require-hashes --no-deps --no-cache`. Verification requires a hash mismatch for that wheel and removes the scratch directory afterward. This test is specific to Molecule. The scenario destroys its own containers at the end.

`ansible/BUILD.bazel` includes the role through its recursive source glob for `Ansible` lint. The YAML gate uses `ryl` with the former `yamllint` rules.

From the repository root, install the pinned collections and run the scenario:

```console
uv run --project ansible ansible-galaxy collection install -r ansible/requirements.yml -p ansible/collections
export ANSIBLE_COLLECTIONS_PATH="$PWD/ansible/collections"
export ANSIBLE_ROLES_PATH="$PWD/ansible/roles"
cd ansible/roles/tbhbops_tool
uv run --project ../.. molecule test -s default
```

Use `molecule destroy -s default` with the same environment if a run stops before cleanup.
