---
description: Agent settings, run requests, and host preparation.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-27T14:44:32Z"
status: stable
tags: [alpha]
title: Agent configuration
type: Reference
---

# Agent configuration

`tbhbdev alpha agent` reads the optional `[agent]` table from the user config. Calling this command requires the table and every key shown below except `[agent.otel]`, `max_run_minutes`, `docker`, `pull_requests_app_client_id`, `pull_requests_app_pem`, the host’s `tbhbdev` path, the `[agent.codex]` table and the `[agent.worker]` table. Unreadable config fails with its path. Missing or invalid settings fail with the path, the full table name and the key, as `[agent.sandbox] audit_dir is required`. Unknown keys fail the same way at any level. `exec` validates the run, checks the remote version and claims the name before it streams the inputs. A tmux session runs the host supervisor. Install that supervisor separately. `wait`, `status`, `kill`, `clean`, `log` and `diff` use the recorded target.

The parser accepts and ignores `host_tag`, `public_repos`, `ssh_connect_timeout`, `ssh_keepalive_seconds`, and `ssh_keepalive_count_max` in existing `[agent]` configs. In `[agent.sandbox]`, it ignores `write_roots`, `denied_home_paths`, `codex_network_proxy_opt_out`, `agy_unsandboxed_opt_out`, `codex_unsandboxed_opt_out`, and the `agy` table, including its `review_read`, `build_read`, and `build_write` keys. It also ignores `unsandboxed` and `proven_harnesses` in each `[[agent.host]]` table, `reviews_app_id` in each credentials entry, and `expires` in each Claude profile.

When the host reports a failed launch before the harness starts, `agent exec` prints the failure and cleans the run. It moves the host files, worktree and controller metadata to trash, and you can reuse the name. Runs that started the harness and launches with an unknown outcome keep their records for inspection.

## Checkouts and publication

`agent exec --fetch-ref REF` accepts `refs/pull/<digits>/head` and `refs/heads/<branch>` for either profile. Repeat the option up to eight times. Branch names use `[A-Za-z0-9._/-]`. Both the controller and host reject `..`, `//`, a leading or trailing slash, a trailing `.lock`, or a leading dash. During preparation the host fetches the refs and the default branch together without passing a credential to the run. Requested refs appear under `origin/fetched/` and don't affect the checkout. Preparation fails if a requested ref is missing. Look for the requested refs in the error.

The launcher warns when the prompt mentions a ref that no `--fetch-ref` option covers.

### Commit checks

The build profile enables `--expect-commit` by default. If the harness succeeds without a new local commit, the run exits 6 and logs `no new commit:`. On success, the host checks that the recorded commit is the tip of `refs/heads/agent/<name>` on origin or an ancestor of that tip. With `--start-ref`, the host checks the selected branch on origin. If the branch is missing or doesn't contain the commit, the run exits 9 and logs `push failed:`. A failed lookup gives the same exit code and log prefix. When the build pushes, the selected branch must still point to the commit fetched during preparation when the build pushes. For research builds, pass `--no-expect-commit` to skip commit verification.

The Stop hook checks the run worktree for uncommitted tracked changes and the run branch for a new commit. In a build with `--start-ref`, the hook accepts a new commit from a rebase. Use `agent exec --no-expect-commit` for a build that can finish without a new commit. Keep the tracked files clean. For reviews, the hook requires a standalone `VERDICT: GO`, `VERDICT: BLOCK` or `VERDICT: NO-GO` line in the final reply, with an optional full commit ID. Before stopping, the hook can require up to three retries per run, then allows the stop and logs the reason.

### Review publication

Use `--ref <full sha>` to launch a review. Include `--reason` for Claude. A missing or empty `--ref` stops the launch before it contacts the host. Leave a valid review draft. For headless reviews without `--schema`, put the verdict on the last line of the final answer. `BLOCK` and `NO-GO` have the same effect. Any commit ID in the verdict must match the checkout. Expect exit code 10 if the harness succeeds and the host can't post the review. In the log, the reason starts with `review not posted:`. Common reasons include a missing verdict, a changed pull request head during approval, or a GitHub error.

### Hook shims

After workspace preparation, a build run reads the repository’s hook configs for references to Bazel shim directories and executables. If a referenced directory is missing, the host reports phase `shims` and uses `bazelisk` or `bazel` to run the `bazel_env` target for each referenced directory’s package, with a 10-minute timeout per target. The harness starts when the required paths exist. A nonzero Bazel status can still leave the paths available. Review runs and preflight checks skip this step.

## Launcher identity

`agent exec` records the launcher and `CLAUDE_CODE_SESSION_ID`. Launches take the name from the tmux pane title with the status glyph removed. Run `agent status` to see `launcher=NAME`. A session with a name can launch a review of any repository, including its own. Use `--role reviewer` or `--profile review`. Set the launching pane's title to its name. Launches fail without a title or `TMUX_PANE`. Briefs specify the review tier.

To launch a review without a tmux pane, set `path` in the optional `[agent.worker]` table to the absolute path to the worker executable. Pass `--worker-workflow ID` using 1 to 128 characters from `[A-Za-z0-9._:-]`. The command walks its parent processes to compare their executable paths with the registered path. It reads `/proc/PID/stat` and `/proc/PID/exe` on Linux and uses `ps` on macOS. A failed lookup or parent loop fails the match. A matching launch appears as `launcher=worker/ID` in status. The flag requires the reviewer role. A tmux pane takes priority. Another process under the same user can match if it runs the executable.

## Host selection and capacity

Set `auto_select = true` in each `[[agent.host]]` table for hosts that can take automatic launches. The setting defaults to `false`, so leave it unset on the lab laptop. With no `--host`, `agent exec` reads each candidate's resources once and picks the usable host with the lowest 1-minute load. The default OS is Linux. Use `--os macos` to select a Mac. When a named host exceeds its limits, the refusal lists usable alternatives from the same OS. To override resource limits, pass `--host` with `--ignore-limits`.

`agent exec` checks resource limits before transferring files. The check refuses a 1-minute load at or over the limit or available memory below either the required share or the reservation. Linux gets memory pressure from `/proc/pressure/memory` (`some avg10`). macOS reads `sysctl -n kern.memorystatus_vm_pressure_level` (1 normal, 2 warn, and 4 critical).

Settings in `[[agent.host]]` control the thresholds. The default `load_limit` equals the logical CPU count and `min_memory_available_percent` defaults to 20. The default pressure limit is 10 for `max_memory_pressure_avg10` on Linux and 4 for `max_memory_pressure_level` on macOS. Both checks refuse at or over their limit. On macOS, use a pressure limit of 1, 2, or 4.

`max_swap_used_percent` defaults to 80. Swap use over the threshold warns when the pressure check succeeds. It refuses if pressure is unknown, such as on a Linux kernel without the pressure file. An unreadable resource check reports `cannot read host limits`. To continue, use `--ignore-limits` or configure `limits_warn_only = true`. The run record saves the flag. Older records omit it and load as `false`.

Each `[[agent.host]]` can set `build_shims` to a Boolean. It defaults to `true`. Set it to `false` on hosts that can't run Bazel. Those hosts refuse builds that need hook shims.

`[agent]` sets the optional memory reservations as positive integers in MiB. `run_memory_mib` sets the build reservation and defaults to 6144 MiB (6 GiB). `review_memory_mib` defaults to the configured build reservation. The launch check also requires available memory to meet the reservation for the requested profile. Linux reads `MemAvailable`. On macOS, the check sums four counts from `vm_stat`, `Pages free`, `Pages inactive`, `Pages speculative`, and `Pages purgeable`, and multiplies the sum by the page size.

Each `[[agent.host]]` can set `max_concurrent_runs` to a non-negative integer. It defaults to 3. Set it to 0 for a host that only runs CI, or to 1 for a host that only runs reviews. The host supervisor reserves a slot from preparation through cleanup. A limit of 3 allows up to 2 builds and leaves a slot for the `review` profile. Reviews can also use any available slots that builds can use. A launch with no eligible slot fails immediately. `--ignore-limits` and `limits_warn_only` apply to the load, memory, pressure, and swap checks. Use the same slot limit in every account's configuration on the host.

## Launch telemetry

`agent exec` exports one launch span named `tbhbdev alpha agent exec`. Its duration covers the launcher, and its launch attributes are `tbhb.agent.host`, `tbhb.agent.harness`, `tbhb.agent.model`, `tbhb.agent.profile`, `tbhb.agent.outcome` and `tbhb.run.id` (the run name). Outcome is `ok` for exit code zero, `refused` for a host-limit refusal and `failed` for another failure. The library accepts strings of at most 128 characters. The library also supplies command and service names, the package version, the exit code, the error type, and a drop counter. The span excludes prompt and request values and SSH output.

The package depends on `tbhb-telemetry` through an exact release URL for its wheel. The lockfile records the wheel's `SHA-256` digest.

`OTEL_EXPORTER_OTLP_ENDPOINT` selects the launch collector and defaults to loopback port 4318. `TBHB_TELEMETRY_ENDPOINT` supplies the endpoint when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset or empty, for launchers that can't pass `OTEL_*` variables. `TBHB_TELEMETRY_ALLOW_HOSTS` accepts a comma-separated list of exact collector host names to permit destinations beyond loopback. These environment variables are independent of `[agent.otel]`, which configures the harness. Export failure is silent, with one send attempt bounded to 250 milliseconds, and leaves the launch's output and exit status intact. Drop records live under `$XDG_STATE_HOME/tbhb/telemetry`, or `~/.local/state/tbhb/telemetry`. Set `OTEL_SDK_DISABLED=true` to turn off launch telemetry.

After applying the endpoint fallback, `agent exec` looks for non-empty process environment values for `OTEL_EXPORTER_OTLP_ENDPOINT` and `TBHB_TELEMETRY_ALLOW_HOSTS`. For each key still missing, it checks the `env` object in Claude Code's managed settings file (`/Library/Application Support/ClaudeCode/managed-settings.json` on macOS or `/etc/claude-code/managed-settings.json` on Linux). It then checks the `env` object in the user's `settings.json` under `$CLAUDE_CONFIG_DIR` or the home directory's `.claude` folder. It looks up only these two keys in the settings files. Once per process, it prints each key's source to stderr and warns that the exporter may drop the launch trace if either key has no source. The line contains no values. `OTEL_SDK_DISABLED=true` skips the lookup and the line.

## Run supervisor

Use the hidden host command as `tbhbdev alpha agent run-host NAME [REQUEST] --host HOST [--prompt FILE] [--preflight] [--owned]`. The driver's tmux start passes `--owned`. With the flag, run-host checks that `<name>.owner`, which the driver wrote, is a private regular file, and doesn't create it. The run ID itself never appears in a command line: the driver sends it as the first line of stdin to each remote script, which compares it with the file. Without the flag, run-host creates the owner file and refuses a name that already has one. Supply a versioned JSON request through `REQUEST`, stdin with `REQUEST` set to `-`, or `<name>.request.json` by leaving out the path. A prompt path defaults to the run's recorded prompt. Config must list the host. During setup, run-host fetches a bare mirror over HTTPS under `$XDG_STATE_HOME/tbhbdev/agent-mirrors/github.com/<owner>/<repo>.git` and creates a separate clone at `<account clone>/.worktrees/agent-<name>`. It reads credentials through the secrets library. It then starts the harness with the policy's explicit environment and records its exit. Run files and the index live under `tbhbdev/agent-runs` in the state directory. SIGHUP, SIGTERM, and SIGINT stop the harness group and refresh Codex auth before run-host writes `<name>.rc`.

At the end of a run, the supervisor lists processes under the agent account before it stops the harness group. On macOS and Linux, it uses process ancestry, harness group membership, or the Bazel workspace path to identify the run's servers. The supervisor accepts workspace paths at or below the run clone root, including checkouts in `.worktrees`. If the process listing fails, it uses the run's recorded output bases.

After the group stops, the supervisor checks that Bazel's process ID file and the server's start time still match the process it found. It sends `SIGKILL` to that process and waits up to 200 milliseconds for it to exit. It logs the base path and result for each attempt, then lists the distinct results in the `bazel_shutdown` field of the completion index entry. If shutdown fails, cleanup continues with the remaining bases and keeps the exit status. The supervisor limits deletion to recorded bases after shutdown.

Every `tbhb-dev` repository on GitHub runs on any host with no list entry, and run-host refuses other repositories before creating a mirror. Git never follows a redirect, so a repository that moved out of the organization fails the fetch. Mirrors fetch from `https://github.com/<owner>/<name>.git` without credentials, prompts, SSH, or token fallback. Existing mirror configs switch to HTTPS on the next run.

The ssh verb `preflight NAME` and the local `--preflight` flag prepare the workspace and credentials and load the signing agent. It logs whether the run worktree has `.bazel_env/bin` and warns that commit hooks fail when the directory is missing, without changing the exit status. After checking the policy and harness command, it writes `preflight ok: <executable path>` to the log. It stops the signing agent and records the exit status, phase, and index entry without launching the harness. Use a fresh name for the next run. Preparation errors use `run preparation failed at <step>: <cause>`. The cause reports the validation message or the first line Git writes to standard error. For an operating system error, it reports the message and the filename without the directory path. Run-host removes control characters and redacts environment values, credentials, and URL user information. The cause has a 200-character limit. Other value errors and git failures without standard error use a generic cause.

Preparation redacts the search path it reports when a hook executable is missing. Check the run log for the failing step and `host_tools.search_path` for the search directories. Install `prek` in a fixed directory on the host tools' search path, or add its directory to `host_path`.

The run clone has its own copy of the mirror's object store. The mirror never prunes objects. Run-host installs the identity and pre-commit hooks from its own templates without running code from the checkout. It finishes all git operations before starting the harness. Plain git in a run clone is unsafe for host processes.

## Harness access

Build and review runs use the full access of the agent account. The account supplies the access boundary for Codex, Claude, and agy.

`tbhbdev alpha install` registers Stop alongside the other user-level hooks for Claude, Codex, and Antigravity. Headless Claude runs use their generated settings and skip user and project settings. Those settings enable hooks for the Stop hook and for one guard hook (see subagent models below). Claude runs use `bypassPermissions`. Headless Codex runs install and trust the Stop hook in their private configuration. Codex marks each run clone as untrusted, which skips its project configuration. Antigravity runs use the installed user-level Stop hook.

A subagent launch should specify its model. Claude enforces this through a hook. Codex and agy use instructions in the brief.

| Harness | Control | Enforcement |
| --- | --- | --- |
| Claude | Settings register the `tbhbdev alpha guard-agent-model` PreToolUse hook with matcher `Agent`. It refuses a launch with no `model`, and a fork when the session's model is costly (an id with `opus` or `fable`) or when the hook can't identify it. A launch can specify any model. Settings `env` also holds `CLAUDE_CODE_FORK_SUBAGENT=0`, which turns forks off. The run keeps subagents: `permissions.deny` doesn't hold `Agent`, and `CLAUDE_CODE_SUBAGENT_MODEL` isn't set. The hook runs the host's `tbhbdev` path, or `.local/bin/tbhbdev` in the account's home. | Enforced by the hook. The hook entry sets `onFailure: "block"`, so a hook that can't start, times out or crashes blocks the launch instead of letting it through. |
| Codex | `-c agents.default_subagent_model=<[agent.codex] subagent_model>` always, and `gpt-6.1-sol` when the key is unset. Subagents stay on. The rendered brief tells the run to set `fork_turns` to `"none"` and name `model` on every `spawn_agent` call, and never to spawn a full-history fork. | The default is a configuration default, not enforcement. The brief's rule is a prompt rule: nothing stops a spawn that omits `model` or `fork_turns`. By the Codex 0.171.0 source, a spawn that omits `model` still gets the configured default, a full-history fork included. No run confirmed this. |
| agy | The rendered brief tells the run not to call `invoke_subagent` or `define_subagent`. | agy lacks per-run enforcement. A subagent defaults to the model of the run. |

### Codex fork behavior

Codex's `spawn_agent` tool takes optional `model`, `reasoning_effort` and `fork_turns` fields. See the Codex 0.171.0 source at tag `rust-v0.171.0` (`codex-rs/core/src/agent/child_config.rs`, `codex-rs/core/src/thread_spawn.rs` and `codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs`):

- An omitted or empty `fork_turns` counts as `"all"`, so the default spawn is a full-history fork. `"none"` starts a fresh child.
- `prepare_agent_spawn_config` applies the requested model, or `agents.default_subagent_model` when the call names none, before the fork-dependent role handling. It does so for a full-history fork and for a fresh spawn alike, and nothing there rejects a model override on a fork. The spawn fails if the model isn't in the spawn tool's catalog of picker-visible models.
- A full-history fork gets the history of its parent, and the model the call names, or the configured default.

Codex's source doesn't say whether a fork can still inherit anything of the parent's model besides the history when `model` and the default are both unset. With the launcher's default always set, that case doesn't arise for runs the launcher starts.

This account of model behavior comes from reading the source. Live verification of the installed binary and a billed subagent launch remain open.

For Codex, the brief says to start children without conversation history and to specify the model. In both cases the configured model remains the default if the call omits it.

agy loads hooks from `~/.gemini/config/hooks.json` and the project's `.agents/hooks.json`. Neither gives the launcher a per-run hook. Skip subagent launches through `run_workflow` or `swarm`.

`[agent.codex] subagent_model` is an optional Codex model ID. `[agent.claude]` has no subagent key, and a config that sets one fails with `not recognized`.

Run-host records phase `unknown` when it can't prove the group stopped and appends cleanup messages to the log. A review with unconfirmed shutdown skips posting.

## Example configuration

```toml
[agent]
launch_wait_seconds = 120
agy_timeout = "900s"
kill_grace_seconds = 10
max_run_minutes = 120
sockets_dir = "/example/sockets"

[agent.sandbox]
network_domains = ["api.github.com", "otlp.example.net"]
# host_path = ["/opt/agent-host/bin"]
audit_dir = ".local/state/example-audit"

[agent.sandbox.path]
linux = [".local/bin", "/usr/local/bin", "/usr/bin", "/bin"]
macos = [".local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin"]

[agent.codex]
web_search = { build = "live", review = "live" }
# subagent_model = "example-model"

[agent.otel]
endpoint = "http://otlp.example.net:4318"

[agent.credentials.agent]
docker = "example-vault/example-docker/config"
reviews_app_client_id = "example-vault/example-app/client_id"
reviews_app_pem = "example-vault/example-app/private_key"
pull_requests_app_client_id = "example-vault/example-pr/client_id"
pull_requests_app_pem = "example-vault/example-pr/private_key"
signing = "example-vault/example-signing/private_key"

# [agent.worker]
# path = "/opt/example-worker/bin/worker"

[agent.claude]
default_profile = "primary"

[agent.claude.profiles.primary]
token = "example/primary/token"

[agent.claude.profiles.secondary]
token = "example/secondary/token"

[[agent.host]]
name = "example-linux"
os = "linux"
shell = "bash"
account = "agent"
accounts = ["agent"]
```

## Tool lookup and credentials

Run-host finds harness executables through the account’s PATH, then `$HOME/.local/bin`, `$HOME/.local/gh-bin`, `$HOME/.bun/bin`, `$HOME/.npm-global/bin`, `/usr/local/bin`, `/opt/homebrew/bin` and the system directories. It uses the run account’s home for these paths. The supervisor also uses `sandbox.path` to find Bazel during shutdown.

Host tools use a fixed search path with optional absolute directories from `host_path`. Harness lookup keeps the `mise/shims` refusal: configure a fixed install directory on the account’s PATH. Remote launch and transfer scripts include the account’s `$HOME/.local/bin`, so they can find tmux there.

Codex uses the account’s existing executable. A missing executable still fails with `host executable codex was not found on PATH <directories>`. Codex uses full access with its network proxy off.

### Credential delivery

Run-host announces full access before preparation. Reviews still require reviews App references and refuse token request failures. A review token holds read permissions only (contents, pull requests, checks, statuses and Actions). Actions read lets reviews read workflow runs in the status rollup for a pull request. Reviews App credentials can remain configured on any run host.

Run-host builds the harness command and adds the brief header. The driver sends the raw brief, an optional `<name>.schema.json`, and `--host` with a configured host name. Codex receives the schema path. Claude receives its contents. An unset, empty or relative `XDG_STATE_HOME` uses the home directory default. Finished runs keep their completion and log files when a rerun fails.

The private `CODEX_HOME` lives at `<state>/tbhbdev/agent-codex/<name>` with mode 0700. Run-host removes it after auth write-back. State defaults to `~/.local/state`.

The opt-in probe checks that the run ignores project configuration. It uses a disposable auth copy and exercises the same refresh and cleanup as a run.

```sh
TBHBDEV_CODEX_TRUST_LIVE=1 uv run pytest tests/integration/alpha/agent/run_host/test_harness_project_trust_live.py
```

## Setting constraints

The settings have these constraints:

- SSH uses a 15-second connect timeout and sends keepalive probes every 30 seconds, with a limit of three unanswered probes. The `[agent.codex]` table and each of its keys are optional, `subagent_model` in it defaults to `gpt-6.1-sol`, and each profile, `build` and `review`, defaults to `live`. A key can narrow it to `cached` or `disabled`.
- `[agent.otel]` is optional and takes one key, `endpoint`, a base URL with no credentials, path, query, or fragment. Use `http` for a private-network collector such as `http://otlp.example.net:4318`, and use `https` elsewhere. For either scheme, the host must appear in `[agent.sandbox] network_domains`. That list matches hosts only, so it limits no port. The parser refuses control characters, a non-numeric, zero, zero-padded or out-of-range port and a malformed authority and quotes nothing from the value in its error. With it set, each Codex run's private `CODEX_HOME` gets a `config.toml` with an `[otel]` table that exports traces to `<endpoint>/v1/traces` and metrics to `<endpoint>/v1/metrics` over `otlp-http`. The table has no headers and sets `log_user_prompt = false`. It leaves the log exporter off, because Codex logs tool arguments and an output preview, which can hold a credential or a file's contents, and `log_user_prompt` guards only the prompt text. Whether to export logs is a later decision. Every run with an endpoint also gets `OTEL_RESOURCE_ATTRIBUTES` with `host.name`, `tbhb.account` and `tbhb.run.id`, and a run without one omits the telemetry table and variable. Tests check the generated file and the variable. A live check against codex-cli 0.167.2 loaded the generated file from a scratch `CODEX_HOME` and accepted it, and the same file with `protocol` set to an invalid value failed with `unknown variant` at `otel.trace_exporter.protocol`, so Codex reads the exporter tables as written. The export itself is unverified end to end, as are two claims about Codex: that it honors the endpoint, and that its traces leave out tool arguments. The private `CODEX_HOME` has mode `0700`. The table contains only the endpoint URL and the prompt flag, so the endpoint's reachability is the boundary.
- `launch_wait_seconds`, `kill_grace_seconds` and `max_run_minutes` are positive integers. `agy_timeout` and the optional `agy_build_timeout` are bare seconds or a duration ending in `s`, `m` or `h`, up to 7 days. An agy review run uses `agy_timeout`, and an agy build run uses `agy_build_timeout`, which defaults to `3600s`. `--timeout` overrides both.
- Path settings are non-blank strings without a null byte or a `..` part. The platform path lists must be non-empty.
- Every credential and profile `token` is a `vault/item/field` reference, and each part starts with a letter or digit, then continues with letters, digits, dots, `_` or `-`. `[agent.credentials.<account>]` takes one table for each account, and `docker` and `gh` are optional. `pull_requests_app_client_id` and `pull_requests_app_pem` are optional too, and a table sets both or neither. A table that sets them can't also set `gh`. A build run whose account has no `gh` requests a GitHub token from that App, limited to the run's repository and to `contents: write`, `pull_requests: write`, and `workflows: write`, and writes it to the run's `gh/hosts.yml`. A build run whose account has neither doesn't get a GitHub credential. If the request fails, the launcher refuses the run with `pull-requests App token unavailable; build refused`. The token expires after an hour, and the launcher neither refreshes nor revokes it.
- Only `signing` also accepts a field label with internal spaces and `?ssh-format=openssh`, which converts the private key to OpenSSH format in memory.
- `default_profile` names one of the profiles, matching without regard to case, so `Primary` selects `primary`. Profile names that differ only in case fail validation. Display uses the name as written.
- `[[agent.host]]` lists at least one host. Its `name` is lower-case, dot-separated labels, `os` is `linux` or `macos`, `shell` is `bash` or `zsh`, `account` must be one of `accounts`, and host names are unique. Every name in `accounts` needs an `[agent.credentials.<name>]` table.

## Run request format

The run request is a versioned JSON object. Version 2 lists every run field and no credential. Readers refuse version 1. A reader refuses unknown versions and fields. It also rejects missing fields. An optional authorization must have the form `owner/repo#N` or `operator-chat-YYYY-MM-DD`. The repo argument takes the form `org/repo` or `<git host>/<org>/<repo>`, with `github.com` as the default git host, and resolves to `Code/<git host>/<org>/<repo>` under the home directory. A repo name can't be `.` or `..` or end in `.git`.

Every `agent exec` request sets `unsandboxed` to true. Omit `--unsandboxed` for the default behavior. Existing callers may still pass `agent exec --unsandboxed operator-chat-YYYY-MM-DD` or `--unsandboxed owner/repo#N` to record `authorized_by`. Run-host accepts `kind = "exec"` and requires `unsandboxed = true`. Run-host reads the account’s config before parsing the request.
