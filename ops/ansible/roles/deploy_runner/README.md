---
type: Readme
title: The deploy runner role
description: The deploy runner role adds keys and a job guard restricted to main to a runner pool slot on its own VM.
tags: [actions-runner, github-actions, ansible, deploy, ci-deploy]
status: draft
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: medium
    surface: exec
    session: sha-256-64:5445955814e1c5c2
    at: "2026-11-15T16:44:18Z"
---

<!-- cspell:words memfd tmpfs -->

Use the `deploy_runner` role with the runner pool. `host-a-deploy` runs the `actions_runner_pool` role with one slot, as the CI virtual machines do, and the pool installs, registers, updates, and configures its runner. Label `tbhb-dev-deploy` and runner group `deploy` set this runner apart. This role adds the job guard to the directory the pool mounts into each job container with `:ro`. Both key pairs stay in a directory on the host. Jobs receive the private keys after the guard admits them. Options are in `meta/argument_specs.yml`.

## Reused pool pieces

See the [README](../actions_runner_pool/README.md) for the pool settings the deploy runner reuses:

- The runner is an ephemeral runner in a container from the pinned runner image. Each start of the slot's `systemd` user unit registers one single-use runner with a configuration generated at startup and runs one job. Updating the runner means a new image digest, `actions_runner_pool_image`, as for the CI virtual machines.
- The registration reads the GitHub App's key through 1Password Connect, with the virtual machine's own Connect token. `actions_runner_pool_op_token_ref` names it, `deploy-fixture / deploy-access-token / token`, and the pool writes the token file from the controller. A token file already in place isn't overwritten.
- The settings are `actions_runner_pool_*` variables in `group_vars/ci_deploy.yml`, with the layout of `group_vars/ci.yml`. Compared with the CI virtual machines' settings, the runner group, label, token, guard hook, slot count, and VM sizes differ.
- The pool's egress table keeps the job containers off private or overlay network addresses except the Connect host.

## Playbook

`ansible/playbooks/deploy-runner-vm.yml` has two plays. The first builds the VM from the `ci_vm` role on `host-a`, on the default `libvirt` network beside `host-a-ci`, as `lab-ci-vms.yml` does for the CI virtual machines. The second runs `docker_rootless`, this role and `actions_runner_pool` on the `ci_deploy` group, the roles of `runner-pool.yml` with this role between them. Check and apply each play on its own, from a Git working tree at current `main`:

```text
cd ansible
mise run playbook -- playbooks/deploy-runner-vm.yml --limit host-a --check --diff
mise run playbook -- playbooks/deploy-runner-vm.yml --limit host-a
mise run playbook -- playbooks/deploy-runner-vm.yml --limit host-a-deploy --check --diff
mise run playbook -- playbooks/deploy-runner-vm.yml --limit host-a-deploy
```

The VM joins the `Tailscale` network with a single-use key at first boot. Between the plays, sign the node key for the VM if the network requires it, and add the virtual machine's host key to `ansible/known_hosts`.

## Network

On `host-a`, the VM uses the default `libvirt` network at the next fixed address after `host-a-ci`. Whether that address is free is unverified. On the host, `ci-vm-egress` drops the virtual machine's traffic to private, shared, link-local and unique local addresses, as it does for the CI VM, and lets public traffic pass. Inside the VM, `tailscale` connects it to the `Tailscale` network. The runner pool applies outbound rules that prevent job containers from reaching private and `Tailscale` addresses except the Connect host. Where a deploy job reaches its target is open: a target on a private or overlay address needs an allow rule in both tables and a `Tailscale` policy grant, and none exists yet.

## Job directory

The role writes `actions_runner_pool_job_dir` under the service user's home, and the pool mounts it at `/etc/actions-runner/job` in each job container using `:ro`. The directory holds:

- `job-guard.sh`, the job-started hook, from `files/job-guard.sh`. The runner requires a hook name ending in `.sh`, `.ps1` or `.js`, so this shim drains the keys with shell built-in commands, which work without Python, and hands them to `job-guard`, run by the image's `python3`. That makes it the role's one shell file, a data file of the role and not a repository tool.
- `job-guard` is the guard, with its settings in `guard.json` and the host key in `known_hosts`. The settings don't specify a key.

A slot exits 78 while the hook file is missing or empty and doesn't start a job, so the pool can't run ahead of this role. The role prints each public key. Add `deploy-key.pub` to the authorized keys of each deploy target, and `repo-key.pub` as a deploy key without write access to the guard's repository.

## Secrets directory

The role writes `actions_runner_pool_job_secrets_dir` under the service user's home, with mode 0700, and the pool mounts it in no container. It holds `deploy-key` and `repo-key` with their `.pub` files. The keys keep the mode 0600 that `ssh-keygen` gives them, and the service user, who created the files, is the one account that can read them.

## Key delivery

The runner's job-started hook isn't an admission gate. Reviewers who read actions/runner v2.338.0 (`JobExtension.cs`, `ScriptHandler.cs` and `StepsRunner.cs`) report that a hook that exits nonzero fails the job's setup step, but a later step with `if: always()` or `if: failure()` still runs, in the same container as the same user. This README relies on that reading, and the proof below tests it. A step can read any key that sits in the container from the start. Keys can't sit in the container from the start, and they arrive after the guard admits the job.

On the host, each start of the slot makes a named pipe in the unit's state directory, with mode 0666. It starts a background writer, `{ tar ... | base64; } >pipe &`. The group's redirect opens the pipe before `tar` reads anything. Until a reader opens the pipe the open waits, and `tar` waits for the reader before opening the files. The writer sends base64, because a shell variable can't hold the zero bytes of a tar stream. Its length stays under the pipe's 64 KiB buffer. Once a reader opens, the writer writes everything and exits. If a reader stops before reaching the end, the last close frees the pipe, and a later opener gets an empty pipe and blocks. Each job container mounts the pipe at `/etc/actions-runner/job-secrets` with `:ro`, and an empty `tmpfs` at `/run/job-secrets`, owned by the runner user with mode 0700. Neither holds a key at the start, and the secrets directory has no other mount.

The hook, `job-guard.sh`, runs before any step. It works under `sh` and under `bash -e -o pipefail`, because the runner may ignore the shebang. Every command that can fail has an `if` or a `||` around it. Its paths are literals, never from the environment, and no key touches the filesystem before admission:

1. Drain. A `while read` loop uses shell built-ins to read the entire pipe into a variable. The keys leave the pipe even if `python3` or `coreutils` is missing, and a later reader, such as a step of a refused job, blocks on an empty pipe. The hook opens the pipe through `exec`, because in both bash and dash the loop can return success after a failed redirect on the loop itself. With no pipe the hook exits nonzero and the guard never starts.
2. Hand over. `printf '%s' "$secrets" | /usr/bin/python3 -I /etc/actions-runner/job/job-guard "$$"`. `printf` is a builtin, which keeps the keys out of every argument list. The hook skips here-documents and here-strings, which a shell may back with a temporary file. The guard gets the shell's process ID. It exits with the shell through `PR_SET_PDEATHSIG`, and refuses unless that shell is still its parent.
3. Admit. The guard decodes the stream, then checks the job and verifies the commit. As its last act it writes `deploy-key` and `repo-key` into `/run/job-secrets` with mode 0600, and exits 0. Then the hook clears its variable and exits 0. For the verification the guard never writes the fetch key to a file. It keeps `repo-key` in a memory file and gives ssh `/proc/<guard pid>/fd/<n>`. Ending the guard at any moment then leaves the fetch key unreadable on disk.
4. Anything else. Any other status, such as a guard that failed, ended on a signal, or never started because `python3` is missing, clears the variable, then truncates through a builtin redirect and removes every file in `/run/job-secrets`, and the hook exits 1. A refused job leaves that directory empty.

The in-memory file system disappears with the container, which the stop phase removes after the job, and delivery through the pipe requires the writer to keep waiting. A runner that never runs the hook is a runner fault, outside this design: the writer would still wait, and the first step would find the pipe loaded. The refusal proof below checks that the hook runs first. The reverse fault, a writer that never opens the pipe because it exited or never started, hangs the hook in its open of the pipe, which has no timeout, until the job's timeout ends it. Deploy workflows set `timeout-minutes` for that reason. Within the guard, each `git` and `ssh` call has a bound, and the hook has no other wait.

Inside the job container, the hook runs as the runner user, who also runs every step. Anything the hook can read, a later step can read, and a step can forge any verdict file that the hook writes for the host. The delivery is a one-shot stream that depends on the hook running first. Once the job starts, the host can't copy a key into the container, because the slot script has become the docker client. A host path that requires root to read is unavailable to the slot script as well, because the slot is a `systemd` user unit. Instead, the secrets directory belongs to the service user, and no container mounts it.

## Job guard

Before every job, the runner calls the hook, and the guard refuses the job, with a reason on standard error, unless these hold:

- Both keys arrive, non-empty, in the stream the shim passes on standard input, and its parent is that shim.
- `GITHUB_REPOSITORY` is `deploy_runner_guard_repository`.
- `GITHUB_REF` is `deploy_runner_guard_ref`.
- `GITHUB_EVENT_NAME` is in `deploy_runner_guard_events`.
- `GITHUB_SHA` is an ancestor of the tip of that reference on the remote.

A fetch with `repo-key`, into a bare repository under `/tmp` in the container, checks the last point. That fetch runs `git` in its own process group, with `ssh -F /dev/null`, the agent off, connection sharing off, and timeouts on. It ends the group on a timeout and at the end. A failed fetch refuses the job, so a missing key or an unreachable remote stops all deploys. Jobs can't change the guard or its settings, because Docker mounts them with `:ro`. As a second layer, the guard exits nonzero on refusal, which fails the hook but admits nothing. In the runner image the guard needs `python3` 3.12, `git` and `ssh`, and in its kernel `memfd_create` and `/proc`. A pinned-image job in `ci.yml` checks the first three in the pinned image, and the fetch key's memory file against `ssh-keygen` and `ssh`. It can't show the runner's own behavior, which the probes below cover.

Any step of an admitted job can read `/run/job-secrets/deploy-key` and `repo-key`, which are reusable SSH credentials. They work from any host with a route to the deploy targets and GitHub until someone rotates them. A step can copy them off the VM, and the role has no control over that.

The item `deploy-access-token` in the `ci-deploy` vault, `deploy-fixture / deploy-access-token / token`, holds the Connect token for the VM. It reads the `runner-fixture` vault, which holds the App's key. It persists on the host in the token file, `.config/actions-runner/op-connect-token` under the service user's home. During each `op read` of the slot's registration, `timeout` and `op` hold it in their environment as `OP_CONNECT_TOKEN`. Neither the container, an argument list, nor the job directory ever sees it, but another process of the service user can read it in that window and from the file. The token belongs to this VM.

## Before the first activation

Nothing here has run as a runner's hook. The `guard-image` job in `ci.yml` runs the guard, the shim and the fetch key's memory file in the pinned image, on a CI slot, but it isn't the runner and says nothing about the probes below. Docker isn't installed on the VM host, so nobody has run the proof below. Before anything routes a deploy to this runner, run it on the VM, from a branch other than `main`, so that the guard refuses. Put this workflow on that branch, then start it with `workflow_dispatch`:

```yaml
name: guard-refusal-proof
on: workflow_dispatch
jobs:
  proof:
    runs-on: [self-hosted, tbhb-dev-deploy]
    steps:
      - name: Normal step, which must never run
        run: echo "RAN: a normal step ran after a refusal"
      - name: always() step
        if: always()
        run: |
          echo "uid: $(id -u)"
          ls -la /run/job-secrets /etc/actions-runner/job-secrets || true
          for path in /run/job-secrets/deploy-key /run/job-secrets/repo-key /etc/actions-runner/job/deploy-key /etc/actions-runner/job/repo-key; do
            if [ -e "$path" ]; then echo "FOUND: $path"; else echo "absent: $path"; fi
          done
          if timeout 5 cat /etc/actions-runner/job-secrets | head -c 1 | grep -q .; then echo "FOUND: data on the pipe"; else echo "absent: nothing on the pipe"; fi
      - name: failure() step
        if: failure()
        run: |
          for path in /run/job-secrets/deploy-key /run/job-secrets/repo-key /etc/actions-runner/job/deploy-key /etc/actions-runner/job/repo-key; do
            if [ -e "$path" ]; then echo "FOUND: $path"; else echo "absent: $path"; fi
          done
          if timeout 5 cat /etc/actions-runner/job-secrets | head -c 1 | grep -q .; then echo "FOUND: data on the pipe"; else echo "absent: nothing on the pipe"; fi
```

Expected output: the job's setup fails with `refusing the job: ref 'refs/heads/...' is not 'refs/heads/main'` from the guard. The `always()` and `failure()` steps run, and both print `absent:` for all four key paths and `absent: nothing on the pipe`, and no step prints `FOUND:` or `RAN:`. The `ls` shows `/run/job-secrets` empty, owned by the runner user (1001) with mode 0700. If the hook step itself blocks the runner, or the always() step prints `FOUND:`, don't activate the runner.

Then run the admit half: dispatch a workflow with one step on `main`, which prints `ls -l /run/job-secrets` and `stat -c %a /run/job-secrets/deploy-key`. Expected: both keys present, mode 600. And confirm with the same step that `/etc/actions-runner/job/deploy-key` is absent and that the runner image has `python3`, `git` and `ssh`.

These items remain unverified until someone runs them on the VM. Run each as a probe before activation, and record the result in the PR that activates the runner:

- [ ] The hook's shell flags. The shim targets both `sh` and `bash --noprofile --norc -e -o pipefail`. A throwaway hook on the probe branch that prints `ps -o args= -p "$$"` and `echo "$-"` shows which one the runner uses.
- [ ] Whether a hook that exits nonzero still lets `if: always()` and `if: failure()` steps run. The preceding refusal proof shows it. Either answer leaves the design sound, but `StepsRunner.cs` leaves that behavior unverified until then.
- [ ] Whether the hook can open the named pipe for reading under the `:ro` bind mount. The preceding admit half shows it: both keys must arrive. A failure there makes every job's hook hang until its timeout.
- [ ] The hook's environment. The job's `env` can set `BASH_ENV` and `LD_PRELOAD`, and a workflow might set `GITHUB_SHA`. In the refusal proof, set each of them at the job level, with a `GITHUB_SHA` set to a commit on `main`, and check that the hook still refuses on the branch and that no step finds a key. The guard runs `python3 -I`, which ignores `PYTHON*` variables but not the loader's.

## Runner group

The runner group `deploy`, id `actions_runner_pool_runner_group_id`, already exists and limits its use to `tbhb-dev/tbhbdev`. The pool's registration puts each runner in the group by its id.
