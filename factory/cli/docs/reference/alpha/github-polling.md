---
description: GitHub event delivery, polling guards, and request budgets.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: GitHub polling
type: Reference
---

# GitHub polling

Agents get GitHub CI and pull request state from awrel, which relays webhook events into a session: subscribe with the repository and the event type, such as `check_run` conclusion events. That's the way to watch GitHub state. Polling GitHub instead burns the account's REST quota, which every agent shares. A hook stops the common polls, and a shim budgets what remains. The budget is a backstop, not the way to watch GitHub state.

- `guard-gh-poll` denies `gh run watch`, `gh run list` and its alias `gh run ls`, with a flag before the verb as in `gh run -R OWNER/REPO list` and with a quoted name such as `$'gh'`, `gh pr checks --watch`, `gh api` or `gh query` with `--paginate`, and reads of the list endpoints `actions/runs`, `actions/workflows/W/runs`, `actions/jobs`, `check-runs` and `check-suites`. It also denies a `while`, `until` or `for` loop that runs `gh`, or runs both `sleep` and a self-matching `pgrep -f`, such as `while pgrep -f fetch.py; do sleep 5; done`, which never ends because `pgrep -f` matches its own shell. A local wait on `pgrep` without `-f`, or with a bracketed pattern such as `pgrep -f '[f]etch.py'`, passes. Single-object reads pass, such as `actions/runs/ID`, `pulls/N`, `commits/SHA/check-runs`, `actions/runs?head_sha=SHA` for one full commit hash, and `rate_limit`. An empty `--method` resets to the default method. The result is a GET unless `-f`, `-F`, `--field`, `--raw-field` or a separate `--input FILE` selects a write. The attached `--input=FILE` isn't read as a write, so `--method= --input=FILE` on a list endpoint draws the poll denial, and `--input FILE` passes. It scans `bash -c`, `eval`, `ssh HOST COMMAND` and a here-document fed to a shell. It also denies `xargs` or `parallel` around `gh run` and unsetting the 1Password service account token's variable. A single read with `gh run list --limit 1` or a list endpoint with `per_page=1` can pass outside a repeated command.
- `gh-shim` budgets the `gh` calls that scripts make, which no hook sees. In an agent session it refuses a call when the root session has made `[gh_budget] hourly_limit` `gh` calls in the past hour, or when the account has fewer than `core_floor` core REST requests left. It checks that with `gh api rate_limit` at most once a minute. A refusal goes to stderr and names awrel and the budget. Otherwise, and always outside an agent session, it runs the real `gh` unchanged. The state is under `$XDG_STATE_HOME/tbhbdev/gh-budget`.

Claude Code gets the shim on PATH from the SessionStart hook, which adds an `export PATH` line to `$CLAUDE_ENV_FILE`. Codex and Antigravity have no setting that prepends to PATH, so start them as `tbhbdev alpha agent-env codex` or `tbhbdev alpha agent-env agy`, followed by the harness's own arguments. A process tree that doesn't inherit that PATH, or a `gh` run by its absolute path, skips the shim. Neither mechanism is a security boundary.
