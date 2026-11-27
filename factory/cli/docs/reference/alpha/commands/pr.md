---
description: Checking, creating, editing, readying, rerunning, closing and rebasing pull requests, and reading their checks.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:55f421bd5e9df8e7
    at: "2026-11-27T03:22:14Z"
status: stable
tags: [alpha]
title: Pull request commands
type: Reference
---

# Pull request commands

`pr check [DRAFT]` checks a draft, and `pr create [DRAFT]` opens a pull request from it.
Both default to `.github/PULL_REQUEST_DRAFT.md`.
`pr edit DRAFT --pr N` updates pull request N and requires the draft path.
It reads the LLM trailers from the pull request's commits on GitHub. If listing those commits fails, it refuses the edit.
`pr ready N [N...]` marks each pull request ready for review if the account the agent's `gh` uses is its GitHub author.
`pr rerun N` reads every page of runs for pull request N's current head commit and reruns the failed jobs of every failed run.
The command skips passing and unfinished runs.
It reports an error when no failed run exists.
It attempts every rerun even if one fails, and returns the exit code from the first failure.
`pr close N --reason TEXT` (or `--comment TEXT`) closes it after the agent-session, open-state and opened-ledger checks, in origin's repository.
For a closed or merged pull request in origin's GitHub repository, `pr delete-branch N` deletes its head branch.
It refuses fork heads, `main`, protected branches, and unreadable metadata.

`tbhbdev alpha pr rebase N [N...]` asks Renovate to rebase open pull requests in the repository named by `origin`.
For pull requests from the Renovate bot, it ticks the rebase checkbox and preserves the rest of each body. It refuses pull requests from other authors.
It skips checked boxes and stops at the first failure.

`tbhbdev alpha pr check` and `pr create` count lines added by `HEAD` after linting the draft. They run a bundled copy of the rules size counter through Bash and `scc` 4.1.0 from the worktree root, without starting Bazel. The commands refresh the required remote-tracking refs on `origin` together and use local refs during a dry run. The limits are 400 added code lines and 1,500 added raw lines. `pr check` prints both counts and their limits. If the counter is unavailable or returns an error or invalid JSON, the command prints `size count unavailable: <reason>` and continues.

If either count exceeds its limit, both commands request a sizing review through `agent exec`. Push the head to `origin` so the reviewer can fetch it. An `OVERRIDE` for that commit lets the commands proceed. If the verdict is `SPLIT`, divide the change into stacked pull requests. When both counts exceed their limits, the problem reports the code count. A dry run reports the excess without starting a review.

The counter compares `HEAD` with the merge base of `HEAD` and the base branch named in the draft. For a draft based on a different branch from the default, the counter finds each branch's merge base with `HEAD` and merges those two commits. It compares the resulting tree with `HEAD`. If the merge conflicts, the counter uses the common ancestor of `HEAD` and the default branch. If `origin`'s default branch is unknown, the counter uses the draft's explicit base alone.

Install `scc` with `mise install` or use the Bazel tool shims. The counter uses the same exemptions as CI for lockfiles, generated files, vendor files, test data, snapshots, and prose. The raw count includes configuration languages and the code count excludes them. `.github/pr-size-exempt.txt` adds repository path globs.

`tbhbdev alpha pr checks N` reads pull request N in the current repository.
It reads checks from the head commit by its hash, including all pages of check runs and commit statuses.
It prints the head commit hash on the first line, then one `SHA name: CONCLUSION` line per check.
Unfinished checks (`PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED` or a pending commit status) print `PENDING`.
Exit code 0 means every check is green: `SUCCESS`, `NEUTRAL` or `SKIPPED`.
Exit code 1 means a failed check or an unrecognized value. Examples include lowercase values and a commit status of `EXPECTED`. The command also returns 1 for a completed check with a missing conclusion.
Exit code 3 means the head has no checks yet, or nothing failed but a check is still unfinished.
A check run that hasn't completed reads its status and never its conclusion.
For commit statuses, green requires `SUCCESS`.
A missing pull request, a failed read or a check with no name exits nonzero with an error.
