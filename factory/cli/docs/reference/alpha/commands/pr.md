---
description: Checking, creating, editing, readying, rerunning, closing and rebasing pull requests, and reading their checks.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:b083225d0bf109b1
    at: "2026-11-27T05:11:27Z"
status: stable
tags: [alpha]
title: Pull request commands
type: Reference
---

# Pull request commands

`pr check [DRAFT]` checks a draft, and `pr create [DRAFT]` opens a pull request from it.
Both default to `.github/PULL_REQUEST_DRAFT.md`.
`pr edit DRAFT --pr N` updates pull request N and requires the draft path.
It reads the LLM trailers from the pull request's commits on GitHub and refuses the edit if it cannot list those commits.
`pr ready N [N...]` marks each one ready for review, only when the pull request's GitHub author is the account the agent's own `gh` runs as.
`pr rerun N` reads every page of runs for pull request N's current head commit and reruns the failed jobs of every failed run.
Passing and unfinished runs are skipped.
It reports an error when no failed run exists.
It attempts every rerun even if one fails, and returns the first failure's exit code.
`pr close N --reason TEXT` (or `--comment TEXT`) closes it after the agent-session, open-state and opened-ledger checks, in origin's repository.
`pr delete-branch N` deletes only the head branch of a closed or merged pull request in origin's GitHub repository.
It refuses fork heads, `main`, protected branches, and unreadable metadata.

`tbhbdev alpha pr rebase N [N...]` asks Renovate to rebase open pull requests in the repository named by `origin`.
It ticks the rebase checkbox only for pull requests from the Renovate bot and preserves the rest of each body.
It skips checked boxes and stops at the first failure.

`tbhbdev alpha pr check` and `pr create` count lines added by `HEAD` after linting the draft. They run a bundled copy of the rules size counter through Bash and `scc` 4.1.0, without starting Bazel. The commands refresh the required remote-tracking refs on `origin` together and use local refs during a dry run. They report added code and raw line counts against limits of 400 code lines and 1,500 raw lines. If either count exceeds its limit, the check fails with a message to split the change into stacked pull requests. When both counts exceed their limits, the problem reports the code count. If the counter is unavailable or returns an error or invalid JSON, the check fails with `size count unavailable: <reason>`, and `pr create` refuses to open the pull request.

The counter compares `HEAD` with the merge base of `HEAD` and the draft's base branch. For a draft based on a different branch from the default, the counter finds each branch's merge base with `HEAD` and merges those two commits. It compares the resulting tree with `HEAD`. If the merge conflicts, the counter uses the common ancestor of `HEAD` and the default branch. If `origin`'s default branch is unknown, the counter uses the draft's explicit base alone.

Install `scc` with `mise install` or use the Bazel tool shims. The counter uses the same exemptions as CI for lockfiles, generated files, vendor files, test data, snapshots, and prose. Configuration languages count only toward the raw limit. `.github/pr-size-exempt.txt` adds repository path globs.

`tbhbdev alpha pr checks N` reads pull request N in the current repository.
It reads checks from the head commit by its hash, including all pages of check runs and commit statuses.
It prints the head commit hash on the first line, then one `SHA name: CONCLUSION` line per check.
Unfinished checks (`PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED` or a pending commit status) print `PENDING`.
Exit code 0 means every check is green: `SUCCESS`, `NEUTRAL` or `SKIPPED`.
Exit code 1 means a failed check or any value the command doesn't know (including lowercase ones, a commit status of `EXPECTED`, or a completed check with no conclusion).
Exit code 3 means the head has no checks yet, or nothing failed but a check is still unfinished.
A check run that hasn't completed reads its status and never its conclusion.
A commit status is green only for `SUCCESS`.
A missing pull request, a failed read or a check with no name exits nonzero with an error.
