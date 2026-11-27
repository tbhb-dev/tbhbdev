---
description: Agent isolation constraints, coordination, and cleanup.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-27T14:44:32Z"
status: stable
tags: [alpha]
title: Agent isolation limits
type: Reference
---

# Agent isolation limits

Run-host runs git in a run clone only during `clone_run` and `create`, before launch. After the harness starts, run-host never runs git in that clone. The controller's `diff` command can read committed work there through the run account's SSH connection. An operator who runs `git add -A` or `git stash` in a run clone can run a nested repository's clean filter. The port never runs those commands in a run clone.

Run-host checks out `agent/NAME` for a build and leaves upstream tracking unset. Each run clone gets a `reference-transaction` hook pinned to the run branch and starting commit. In prepared transactions, it refuses updates to other local branches. Each new run branch tip must descend from the start unless the build uses `--start-ref`. Git's files backend can rename a branch without reporting a prepared transaction. The reference-transaction hook permits deletion of the run branch. It also permits updates to tags, remote-tracking refs, the stash, and detached `HEAD`. The pre-push hook allows the run branch as the branch destination, or the branch that `--start-ref` specifies. Run-host turns off hooks for its own Git commands. The legacy sandbox probes prevent writes to `.git/config`. In those probes, use `git switch --no-track -c x origin/main` and push with an explicit destination branch, such as `git push origin HEAD:refs/heads/x`.

Every `agent exec` run has the account’s full access. It can read the account’s 1Password Connect token and every secret the account can read, and write anything the account can write. Codex, Claude and agy use this behavior for both build and review profiles. The agent account supplies the boundary. A child can leave the process group and keep running after the run ends.
Runs skip sandbox policy checks, executable trust checks and write-root audits. They still refuse mise shims.

- A run can read other runs' temp directories.
- Build runs have access to the account's `gh` token and Docker config.
- A run's tools can read its private Codex auth file.
- The agent can write the final answer file.
- Run-host sets Codex's `web_search` mode and removes the private `CODEX_HOME` at finish. It has no separate web search store or cleanup for any state that Codex or its provider keeps outside that home.

The coordinator traced every git process that the Codex parent starts, with every Git plant from the regression tests present. With codex-cli 0.166.0 on macOS and 0.164.0 on Linux, each session started 12 git processes before any tool call: `rev-parse HEAD` (3), `rev-parse --git-dir` (2), `rev-parse --abbrev-ref HEAD` (2), `remote get-url origin` (2), `remote -v` (1), `config --null --get core.fsmonitor` (1) and `status --porcelain` (1). Each one used Codex's own `-c safe.bareRepository=explicit -c core.hooksPath=/dev/null -c core.fsmonitor=false`. None started after a tool call. Version 0.164.0 also cloned its plugin repository into `CODEX_HOME`'s temp directory and ran `git maintenance run --auto` there. Re-check these observations on every Codex upgrade.

The legacy sandbox probes also exercise Git pointer files and terminal isolation. They test the policy builders separately from `agent exec`, which launches every harness without a sandbox.

Launch a run with a local prompt file:

```sh
tbhbdev alpha agent exec --host example-linux --repo example-org/example-repo --model example-model --effort high --profile build example brief.md
tbhbdev alpha agent status example
tbhbdev alpha agent wait example
```

Reviews accept `--ref` and resolve it before launch. `--schema` streams a JSON schema beside the prompt. Claude requires `--reason` and accepts `--claude-profile`. Use a `gemini-*` model for agy, or pass `--allow-non-gemini`. `--ops` selects the host's configured `ops` account.

Each run gets a private `TMPDIR`. Run-host sets `PYTEST_DEBUG_TEMPROOT` to the same directory for pytest's default base directory. At exit, run-host removes the run's temporary tree without following symlinks. If removal fails, the run keeps its exit status.

The optional `[[agent.host]] tbhbdev` key names an absolute executable path on that host. Its default is `$HOME/.local/bin/tbhbdev`, using the remote account’s home. Local `ssh` and `tmux` resolve against a fixed system PATH. Remote launch and transfer scripts add the account’s `$HOME/.local/bin` and the standard package directories on macOS, and skip login shell startup.

The coordinator stores metadata under `$XDG_STATE_HOME/tbhbdev/agent-runs/` on the coordinator. The remote directory uses the same location under the run account, with `.local/state` under home as the default state directory. The transfer claims `<name>.owner` with `set -C` before extracting inputs. It refuses an existing session or any `<name>.*` file with exit status 73. A collision never kills the existing session.

The tmux socket is `-S <sockets_dir>/agents`. The driver creates the configured directory with mode 0700 and refuses a symlink, another owner, or any other mode. Sandbox policies deny reads there. Whether each sandbox blocks a Unix socket connection needs the shadow test.

`wait` prefers the final answer and returns the run's exit status. It polls every 30 seconds. Consecutive connection failures stop after 240 seconds with exit status 75. Exit codes 1 and 75 overlap with harness exit codes. Read the first result line to distinguish an unknown or unreachable run from a harness result. Agent text follows that line.

The build pushes its branch and opens any new pull request from the harness. A build with `--start-ref` pushes to the remote branch from that option.

| Exit status | Result |
| --- | --- |
| 0 | The build meets its commit and push requirements, or the host posts the review. |
| 4 | The commit check fails. The log starts the reason line with `commit check failed:`. |
| 6 | The build produced no new commit with `--expect-commit` enabled by default. The log starts the reason line with `no new commit:`. |
| 9 | The destination branch lacks the build commit on origin, or the host couldn't verify it. The log starts the reason line with `push failed:`. |
| 10 | The harness succeeded but the host did not post a review. The log starts the reason line with `review not posted:`. |

The commit check compares local branch tips and detached `HEAD` with the refs recorded before the harness started, including refs fetched during preparation. It requires new history to descend from the starting commit, with an exception for the run branch in a `--start-ref` build. On an ancestry failure, it records the count and short commit IDs in the log. If the harness leaves the run branch checked out and other branches contain new commits, the check requires new history on the run branch. When a build requires a commit, run-host verifies that the destination branch on origin contains the recorded commit.

If a build harness fails, run-host keeps its exit status, log, and worktree. Committed work remains available for patch export, including work at the current checkout when the run branch has no new commit. `tbhbdev alpha agent status NAME` shows the last recorded reason next to a nonzero return code when the log contains a reason.

Each claim writes a random run ID to the local metadata and the remote `<name>.owner` file before extracting inputs. Every remote command reads the ID from the first line of its stdin and compares it with `<name>.owner`, so the ID never appears in an ssh argument list or a host audit log. The tmux start passes run-host `--owned`, and run-host checks `<name>.owner` first. The file must be a regular file, never a symlink. Its mode must be 0600 and its owner the running user. Its content must be the ID followed by a newline. An empty ID, a missing file, a directory, a named pipe or any other content refuses the start with exit status 1 and leaves the earlier run's files untouched. Run-host writes its log and return code for a failed start only while it holds `<name>.run.lock`. A refusal before that point leaves every record unchanged. Later commands check that ID before they act. A mismatch or missing owner archives local metadata and reports `not owner`. After a transfer error, the driver retains metadata only when a remote owner check succeeds. The driver sends the raw brief and starts run-host with the configured `--host`. Every tmux call uses `-f /dev/null`, and session creation passes an absolute executable with separate arguments.

`kill` closes the tmux session to notify the supervisor with SIGHUP. The supervisor stops its children before it records the return code. The coordinator writes 137 only after the session closes and the grace period elapses without a return code. It also writes `<name>.rc.fallback`, which makes `clean` require `--force` because processes might still run.

The host keeps a bare mirror at `$XDG_STATE_HOME/tbhbdev/agent-mirrors/<git host>/<owner>/<repo>.git`. An empty or unset `XDG_STATE_HOME` uses `~/.local/state`. Only host git touches the mirror, under its `agent-mirror.lock`. Before launch, the supervisor makes a separate run clone at `<account clone>/.worktrees/agent-<name>`. That clone has its own `.git` directory. The legacy sandbox denies writes to `.git/config`, `.git/hooks` and `.git/info`. Inspection output from both repositories is untrusted.

```text
Don't run git inside a run clone: it is agent-written. Use `tbhbdev alpha agent log|diff NAME`.
```

For a run with a nonzero return code, `log NAME` shows the last 65536 bytes of its host log. Otherwise, `log NAME` shows the final output or output stream for a full-access run. For older sandboxed runs, it shows up to ten pushed commits by default. Use `log -n 5 NAME` to change the count.

When `agent/NAME` exists on origin, `diff NAME` shows changes from the default branch's merge base to that pushed branch. For a failed build, an unpublished branch, or an unavailable origin, build inspection reads the run clone. This uses the recorded starting commit and tip when available. Older runs can resolve the starting commit from the first entry in Git's reference log for the run branch and the tip from `HEAD`.

For a finished build, `tbhbdev alpha agent diff NAME --patch > run.mbox` exports the committed changes as a complete `mbox` file without contacting origin. Apply the series with `git am --keep-cr run.mbox`. The export skips uncommitted changes. A build with no new commits produces empty output.

Local inspection and export use a temporary bare repository that borrows only the run's object directory. They read commit IDs as data and skip the run's Git configuration. They refuse symlinks along the clone path and in the Git metadata they read. The reads also fail when Git uses object alternates or a multi-pack index, or when Git can't read the object or ref directories. The symlink scan and read can race with a running harness. Both paths turn off external diff, text conversion, and color. The command caps display output at 65536 bytes and filters terminal controls. Patch export preserves the file bytes. `wait` and `status` use the run's state files. The mirror never prunes, so disk use grows with history until a pruning follow-up.

Cleanup refuses an open session. `wait` keeps the worktree after the run ends. Cleaning an owned run requires `clean --force` because cleanup cannot verify that all commits reached origin or that the output was read. Read the output and verify pushed commits before forcing cleanup. The command moves the run clone and per-run files into dated trash without reading agent-written git state. Cleanup refuses a symlink in place of the run clone. Retries skip completed moves, and removing all files for the run permits name reuse. `keep` succeeds when the marker already exists. For full-access review runs, `diff` prints the final output or output stream from the host.

Ref resolution runs outside the clone against the SSH URL from the repository argument. Remote Git uses an empty environment and explicit overrides for hooks, file monitoring, SSH, credentials, prompts, external protocols, paging, submodules, and automatic maintenance. It disables lazy fetching. Remote scripts use the C locale. Run file reads refuse symlinks, bound byte counts, check status fields, replace invalid UTF-8, and remove terminal control characters. Polls wait at least half a second between connections.

## Risk

The driver’s mirror receives agent-pushed objects.

One successful, real `agent exec` run on a host proves that harness. `proven_harnesses` records this fact without refusing unproven harnesses. An empty list never refuses a run. Remove the recorded fact when upgrading that harness. Codex uses the existing account executable on PATH and requires no standalone release layout.
