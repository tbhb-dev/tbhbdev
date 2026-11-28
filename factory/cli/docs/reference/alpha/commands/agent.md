---
description: Run coordination, host checks, fetch refs, and build run expectations.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-27T14:44:32Z"
status: stable
tags: [alpha]
title: Agent
type: Reference
---

# Agent

`agent` coordinates runs with `exec`, `send`, `wait`, `status`, `log`, `diff`, `kill`, `keep`, and `clean`.

`signing-parity` checks whether GitHub lists the signing keys for the configured accounts.

`heal HOST...` brings each host to the latest command-line tool build on `main` without checking limits or launching a run, and prints one line per host (`HOST: before <version> after <version>` or `HOST: heal failed: <cause>`). It exits nonzero if any host fails or is unknown.

## Launch and steer

`tbhbdev alpha agent exec` checks the brief and any `--schema` file before contacting the run host.
It refuses either input with a 1Password reference (`op://`), a private key header, or any of the token prefixes `ghp_`, `github_pat_`, `sk-ant-`, `AKIA` or `xoxb-`.
It exits nonzero and reports the input and the refusal kind without including the matched text.

Launch a Codex build with `agent exec --steer` to accept messages while its turn is active.
Run `agent send RUN "MESSAGE"` to steer that turn. `send` applies the same credential check as `exec` before contacting the run host.
Steering requires a build without `--schema`. If the run is unknown or its thread has no active turn, `send` returns an error.
`agent send --harness claude RUN "MESSAGE"` prints `Claude delivery is not built` and exits with status 1.

## Version and launch options

When `tbhbdev alpha agent exec` evaluates a candidate host, it reads the host's installed command-line tool version and heals a mismatch before checking limits, admitted runs, or slots.
It looks up the head of `tbhb-dev/tbhbdev` on `main` and installs that commit on each side that differs, at most once per host per driver invocation.
If a heal fails, the host's refusal message includes `heal failed: <cause>`, and automatic selection continues to the next candidate host.
It logs each successful install to standard error.
If it updates the driver, it re-executes the command with the original arguments.
The retry reads the host version again.
If the versions still differ, it reports the original mismatch with `after one sync` and refuses the launch.

`tbhbdev alpha agent exec --fetch-ref REF` makes `refs/heads/<branch>` available as `origin/fetched/heads/<branch>` and `refs/pull/N/head` as `origin/fetched/pull/N/head`.
Repeat `--fetch-ref` up to eight times with either profile.

`tbhbdev alpha agent exec --worker-workflow ID` launches a review as the registered worker `worker/ID` when the caller's identity is unavailable from a tmux pane.
See [Agent configuration](../agent-configuration.md) for the `[agent.worker]` table that registers it.

## Build publication

Build runs expect a new commit and an updated origin branch by default.
Pass `--no-expect-commit` for a research run that can finish without committing or pushing.
After a successful harness exit, `agent wait` returns 6 for a missing commit, 9 for an unchanged or unverifiable run branch on origin, and 10 if the host fails to post a review.
Use `agent status NAME` to see the reason.

For a build that updates an existing pull request, pass both `--fetch-ref refs/heads/<branch>` and `--start-ref refs/heads/<branch>`.
The run starts at the fetched commit and pushes to the existing pull request branch.
The command refuses the repository's default branch.

A build run that opens a pull request writes `.github/PULL_REQUEST_DRAFT.md` in its worktree and keeps it out of commits.
Set `base` in the draft's frontmatter to identify the target branch.
After pushing its `agent/` branch to origin, the run calls `tbhbdev alpha pr create` from the worktree.
`pr create` checks the draft and adds the commit trailers and the opener's identity to the body.
Once GitHub opens the pull request, the command records it in the opened ledger.
CI fails a pull request whose diff contains the draft.

## Rewriting the start branch

To push rewritten history, use `--force-with-lease=refs/heads/<branch>:<sha>` with the commit ID from `origin/fetched/heads/<branch>`.
Git refuses the push if the remote branch has moved since the fetch.
