---
type: Design Draft
title: GitHub read commands
description: GitHub queries, compact output, event waits, and one rule for agent use of the command-line tool.
tags: [alpha, design, github]
status: draft
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:2aabefedb54f3e1d
    at: "2026-11-27T19:56:38Z"
---

# GitHub read commands

| Status | Owner | Updated | Needs from tbhb |
| --- | --- | --- | --- |
| Draft | Tool maintainer | 2026-11-27 | None |

## Summary

Agents read the same pull request (PR) to learn its head commit, status, and checks. The [private call analysis](#appendix-a-evidence) attributes an estimated 89% of recorded GitHub calls to PR reads.

Add `tbhbdev alpha read`. Give it compact output, a combined PR view, and a bounded `wait` that uses webhook events where available.

Deploy the commands before enforcing one rule for agent use of `gh`. Keep `gh query` for one-off reads that the poll guard permits and preserve the operator's write controls.

## Decisions for tbhb

The operator has settled every decision.

## Already decided

The operator's decision record of 2026-11-27 (GitHub read commands: credential and enforcement) settles two questions.

- **Credential.** Read commands use a repository-scoped token with the review run's read permissions. Build runs get a separate read token from the existing App. This uses existing accounts, Apps, and capabilities. If the operator reverses the decision, limit the commands to sessions with an existing read credential.
- **Enforcement.** After deployment, agents must use the commands for reads they cover. The rule first records what it would refuse on each host. Then enable enforcement on that host. If the operator reverses the decision, keep the three existing `gh` hooks and make the commands voluntary.
- The tool maintainer puts all reads under `read` and keeps the existing write commands. They maintain the output and cache defaults. [Alpha command reference](../reference/alpha/commands.md)
- Use the [event-first rule](../reference/alpha/github-polling.md) and the blocking interface of [agent wait](../reference/alpha/commands/agent.md) as precedents for the tool maintainer.
- Use REST and share budget accounting with the shim. The tool maintainer follows the precedent for conditional requests in the [GitHub metrics decision](../../../github-metrics/docs/architecture/decisions/0008-the-service-reads-github-through-rest-with-condi.md).

## Proposal

### Command set

Register `read` in alpha help and `COMMANDS`. See the [command table](#command-fields-and-refusal-hints).

The read commands are a package named `read` directly under `factory/cli/src/tbhbdev/`, not under `alpha/`, and their tests go under `factory/cli/tests/unit/read/`. The command module holds arguments, output, and exit status. Handler modules do the work and import no argument parsing. The alpha dispatcher binds `read` by name, and command names stay `tbhbdev alpha read ...`.

Build `read pr state <pr>` with `--repo` and `--fields` first. The second build adds `read pr show [<pr|sha>]`, which resolves the head once and reads state and checks. Add files, commits, and reviews on request with `--files`, `--commits`, and `--reviews`.

The combined view covers 1,993 state-and-check pairs and 803 repeated groups. A query language adds parsing work.

Use a positive decimal argument as a PR number. Resolve a full or abbreviated hexadecimal commit ID through GitHub, and spell an all-digit commit ID as `sha:<sha>`. A PR view resolves a commit to the unique associated PR with that exact head, or asks for a PR number if it finds more than one.

Without an argument in a clone, pick the unique open PR for the current branch and head repository. Require an argument outside a clone or on a detached head. Take the repository from the clone's `origin`, including for SSH, unless the caller gives `--repo <owner>/<repo>` or a positional repository.

Move `alpha pr checks` to `alpha read pr checks`. Keep a temporary alias with the old output until callers migrate. Drafts still use `alpha pr check`.

Use the [escape hatch](#guard) for comments, releases, search, GraphQL reads, auth inspection, and raw REST when the poll guard allows those reads.

### Agent output

Print one record per line. Separate fields with tabs and omit color, decoration, and headers by default. The `--header` flag adds labels, and `--fields <name,name>` prints columns in the requested order, or prints the bare escaped value if the caller picks one field.

Escape backslashes, tabs, and line breaks within a field. Use `none` for an absent value, `\none` for the literal string, and an empty field for the empty string. Return 0 with empty output for an empty list or 3 when checks are absent.

Use compact JSON Lines for `--json`. Document field types and order per command and use `null` for absence. In the combined view, put the section name before each tab-separated record and place `kind` first in each JSON object.

Pretty-printed JSON repeats labels and adds indentation, so reserve labels for JSON mode or `--header`.

Preserve commit and log order. Sort other lists by stable keys such as path or check name and ID. In text mode put structured fields in compact JSON cells, and in JSON mode use native values.

Cap each text field at 2 KiB and each command's stdout at 32 KiB by default. Cut at a character boundary. Reserve room within the cap for a marker on the last line, or a final JSON record with `truncated` details, that specifies the field and gives the command to get the rest.

Use `--offset <bytes>` with one selected text field on one record, or with a log stream. Count bytes before escaping. Use `--after <cursor>` to retrieve more records. The cursor binds to the head and filters.

If GitHub omits a patch, report that upstream limit.

| Exit | Meaning |
| ---: | --- |
| 0 | Read succeeded, checks are green, or the wait condition holds |
| 1 | Checks failed or have an unknown outcome |
| 2 | Usage error, including an ambiguous target |
| 3 | Checks are pending or absent |
| 4 | Target absent or hidden by GitHub |
| 5 | Wait deadline elapsed |
| 6 | Tool, auth, network, or rate budget error |
| 7 | Pinned PR head changed during the wait |
| 8 | PR closed without merging during a merge wait |

### Waiting

Offer `read wait checks [<pr|sha>] --timeout <duration>` and `read wait pr [<pr|sha>] --until merged|closed --timeout <duration>`. Require a positive timeout. One monotonic deadline covers subscription, requests, and retries.

Subscribe through awrel, the webhook relay, before the first fresh read so the command can catch events during that read. Use repository and pinned head for check and status events, or repository and number for PR events. A checks wait reports an outcome once all checks and statuses finish.

An event prompts one fresh read, including a check that the PR head matches. Return 7 for a changed head during a checks wait on a PR.

Events can arrive late or go missing. Reserve time and request slots for one final fresh read as the deadline approaches.

If the relay is unavailable or disconnects, start at 5 seconds and double the delay up to 60 seconds, with random variation. Cap the wait at 20 GitHub requests, including pages and the final read. Save the remaining slots for that read if another attempt would exceed the cap.

Fallback requests require the poll rule change in [Guard](#guard). They stay within the selected PR or commit and use the same read credential and request budget.

Honor `Retry-After` and quota resets within the deadline. Return 5 if the condition remains unresolved at the deadline, or 6 with the retry time for an ordinary query blocked by the rate limit. Each request counts locally, including a `304 Not Modified` with valid authorization, which GitHub exempts from the primary REST quota.

One bounded call replaces a shell loop or `gh run watch`.

## How it works

### Caching and rate limits

Keep a private cache in local state for each run or interactive session. Index it by credential scope, repository, endpoint, exact query, and representation. Store the response by its entity tag (`ETag`), the server's version identifier, with a 15-second expiry.

Revalidate with `If-None-Match`. A wait can keep a response in memory to revalidate during backoff, but event and deadline decisions require an unconditional read. Bypass the shared cache for those decisions, merge state before a merge, and active job logs.

Direct REST calls bypass the shim. Share its locked budget for each run or interactive root session, and partition quota samples by credential scope. Charge each outgoing GET before sending it, including pages and retries, and apply the existing core quota floor.

Cache hits are free. Count each shim invocation once because it has no view of the requests inside `gh`. Separate budgets would let a run spend both.

### Auth

Use a repository-scoped token with `REVIEW_PERMISSIONS` from `agent/credentials.py`. That set gives read access to contents, pull requests, checks, statuses, and actions.

Review runs reuse their read token from the private `GH_CONFIG_DIR` handoff. Build runs use a separate token with the same read permissions.

For interactive use, request a repository-scoped token from the existing App with `alpha gh-app --permissions`. Check the credential scope. A session without a suitable read credential gets exit 6 with a setup message.

Use direct REST for pagination, cache validation, and request accounting. The existing credential provider handles token exchange outside the read transport. Enforce GET, forbid request bodies, and test method rejection before any network request.

Read both check runs and commit statuses at the resolved head. Use the latest entry per source and context. Preserve the check-run green outcomes, require success for green status contexts, and give failure precedence in a one-time read.

### Guard

Replace the three hooks with `guard-gh`. Share its GitHub rules with the shim instead of parsing shell text in each hook.

Agents may use these forms.

- `gh auth status` for inspection. Turn token display off.
- `gh query` for one-off reads beyond the supported views, including comments, releases, search, and raw REST. Apply the [existing poll restrictions](../reference/alpha/github-polling.md) and accept read operations. Reject GraphQL mutations, opaque input, and requests with more than one GraphQL operation.
- `gh release download` for a named release and asset.
- `gh pr review --comment` for direct feedback where no alpha command exists. Route approving reviews through review runs and require explicit entries for any further writes.

Deny agents multi-run listings.

Keep denying `--paginate` on `gh api` and `gh query` and bulk reads of `actions/runs`, `actions/workflows/W/runs`, `actions/jobs`, `check-runs`, and `check-suites`.

For run state, use the single-run reads the poll guard already passes.

- `actions/runs/ID` for a known run.
- `actions/runs?head_sha=SHA` for one full commit hash.
- `gh run list --limit 1` or `per_page=1` for one result.

Use `read run jobs` for a run's jobs and `read wait checks` to wait for checks.

Give refused reads the [replacement command and flags](#command-fields-and-refusal-hints), and give writes their existing alpha command. Allow projections the replacement lacks.

Scan commands in pipes, loops, and shell groups in parentheses, including those in literal `bash -c` text and remote commands passed to `ssh`. Refuse repeated GitHub calls in visible loops and point to `read wait` where it fits. On agent hosts the PATH shim checks the actual arguments, config, and budget, including scripts the hook sees as a filename.

Refuse changes to the assigned `GH_CONFIG_DIR`. Alpha write commands use the internal invocation path after validation. Human terminal calls outside agent sessions pass through.

Keep the 1Password token-unset check, `verdict` in `factory/cli/src/tbhbdev/alpha/guard/git_hooks.py` (called from `guard/gh_poll/command.py`), and the self-matching `pgrep -f` loop check. The operator's write controls in `guard-gh-pr` stay unchanged. The guard code is already split: `guard/shell_commands.py` scans shell commands, `guard/gh_options.py` and `guard/options.py` parse `gh` options, `guard/gh_poll/decision.py` and `guard/gh_poll/token.py` hold the poll rules, and `guard/gh_pr/decision.py` holds the PR write rules. The unified hook reuses and extends these modules and keeps each under 400 code lines. It does not create parallel ones.

The owner of the poll guard defines the change to the standing polling rule for fallback requests inside one bounded wait.

Limit fallback to repeated reads of the selected PR or commit within the request cap and deadline. Keep existing credential scopes and denials for bulk reads and shell loops.

Deploy replacements and the escape hatch first. Record verdicts before enforcing them on each host. Restore the old policy if a replacement fails.

The operator exports corpus entries locally, replaces identifiers, paths, and private arguments with placeholders, and deduplicates by normalized shell form and arguments. The tool maintainer commits reviewed fixtures with one verdict and one replacement or empty hint per entry. Add fixtures for newly seen forms and replay them through both the hook and shim.

### Measurement

Use `telemetry.py` for the commands, hook, and shim. Record the command family, path taken, GitHub request count, cache result, outcome, and output bytes. Count a refusal at the layer that refused it and match host and run coverage across measurement windows.

| Metric | Baseline | Target |
| --- | --- | --- |
| Calls per merged PR | 43,389 observed invocations. The merge count for that window is unknown. | 50% fewer GitHub operations per merge. Count REST attempts plus the legacy invocation proxy over the same window as unique merged PR events. Publish both numerator parts. |
| Raw `gh` share | All 43,389 sampled invocations use the raw path, including 78% through `gh query`. The denominator across existing alpha commands is unknown. | At most 15% of GitHub-facing agent invocations, including the escape hatch in the numerator. Count an alpha command once even if it uses a subprocess. |
| Refusals per day | About 907 in the interactive sample. The number of observed days and headless refusals are unknown. | 75% fewer refusals per observed day at comparable agent volume. Count command policy refusals across instrumented hosts, with separate reason counts. |
| Estimated read tokens | 43,209 audit reads, with payload sizes unknown. | 50% fewer estimated tokens per merge. Estimate from read bytes returned to the agent, including any saved command output the agent retrieves and the instrumentation can count. |

Replace the table's baselines with measurements from the week before enforcement. Compare that week with a week after adoption. Deduplicate merge events and report gaps in telemetry and event coverage. Use the same bytes-per-token method in both windows and sample filtered legacy output to avoid inflating the baseline.

Raw invocation counts miss part of the cost. A combined command can replace paired shell calls and still make the same number of REST requests.

## Risks and unknowns

| Risk or unknown | Effect | What handles it | Evidence |
| --- | --- | --- | --- |
| Existing CI status semantics differ across API surfaces | A check could appear green before it finishes | Compare checks and status contexts with the old rollup, including missing checks and mixed outcomes | Existing `pull_request/checks.py` and `pull_request/check_results.py` |
| Relay loses events | The wait might detect completion at the deadline | Subscribe before reading and re-read after signals and at the deadline | [Existing relay use](../reference/alpha/github-polling.md) |
| Interactive auth and rules access vary | Some reads may require setup or the escape hatch | Test the review permission set against each endpoint and deploy the repository-scoped read credential before enforcement | Existing review credentials |
| A script skips the shim or runs remotely with a different PATH | Its direct `gh` command may escape local argument checks | Install the shim on agent hosts and check the remote entry points. Keep GitHub credential controls in force. | Existing shim docs |
| GitHub caps file lists, trees, and patches | A result can omit data even after local pagination | Report upstream caps and give the applicable Git or narrow REST retrieval path | Existing REST API |

## Plan

Each step is a PR under 400 code lines and 1,500 raw lines. The tool maintainer runs each check against a fixture repository or stubbed responses.

| Step | What lands | Done when |
| --- | --- | --- |
| 1 | `pr state` with `--fields`, GET transport, read auth, and a shared budget reservation per GET | Run `read pr state <pr> --fields head_sha` against a known head. It returns the bare commit ID with one GET by PR number. |
| 2 | `pr checks` and combined `pr show`, plus commit selection (`sha:` and hexadecimal IDs), current-branch selection, the repository from `origin`, and the state flags `--merge`, `--size`, and `--body` | Run both on green, failed, and pending fixture heads, including legacy statuses. Exit codes are 0, 1, and 3, and the combined request resolves the head once. |
| 3 | `pr files` and output continuation, plus `--json` and `--header` | Run the files read against two pages with a large patch. Retrieve the rest of the patch and the next page, then run the same view via `pr show`. |
| 4 | `pr commits` and `pr reviews` | Run both reads directly and via `pr show`. Verify commit trailers and approvals at the current head. |
| 5 | `repo info` and `repo file` | Query a branch's rules and a file at a known ref. Compare the tree and decoded bytes with Git. |
| 6 | `commit show` and `pr list` | Run compare and branch-filtered PR list queries against fixture refs. Check the resolved commit ID and PR number. |
| 7 | `run jobs` and `run log` | Query a fixture with two jobs and fetch a failed job's log in chunks. Reassemble it and compare it byte for byte. |
| 8 | Cache revalidation and query telemetry | Run two clients against a fixture that returns 304 and a rate-limit response. Confirm cache hits leave the request counter unchanged. Both clients draw from a shared budget. |
| 9 | `wait checks` and `wait pr` | Replay a close event, head change, missing event, and relay outage. Verify fresh reads, exit codes, call cap, and the deadline. |
| 10 | Shared argument rules, extending the existing `guard/` modules, and sanitized fixtures in record mode. Record the fallback rule that the owner of the poll guard defines. Deliver a read-only token to build runs, and check the credential scope. | Replay the corpus through the table matcher and check the verdicts and hints. Check that recorded verdicts restrict fallback to the selected PR or commit within one bounded wait. Existing hooks still enforce the old rules. A build run's read command uses a token with `REVIEW_PERMISSIONS` only, and a token without them exits 6. |
| 11 | Unified shell hook and shim enforcement, including the fallback rule after approval from the owner of the poll guard | Replay the corpus at both entry points and run every replacement on each host before enabling its refusal. Verify that fallback respects the request cap and deadline, shell polling and bulk reads still draw refusals, and human passthrough still works. |

## Appendix `A`: evidence

The call figures come from an analysis of recorded agent sessions. The repository lacks the session records needed to reproduce it. The [Measurement](#measurement) section replaces these baselines with measured ones in the week before enforcement.

### Sources and current behavior

- **Existing.** Repository facts refer to `origin/main` at `79609f48d4d1bb75518621bb82098a483d9a9098`. See `factory/cli/src/tbhbdev/alpha/command.py`, the `factory/cli/src/tbhbdev/alpha/pull_request/` package (the checks read is in `pull_request/checks.py`, where the GraphQL call starts at line 15, and the outcome rules are in `pull_request/check_results.py`), `gh_app.py`, `agent/credentials.py`, `gh_shim.py`, and the [command references](../reference/alpha/commands.md).
- **Existing.** PR tooling calls `gh` via `find_tool("gh")`. `pr checks N` prints the head commit ID and check names with outcomes. Its paginated `gh api graphql` read targets that commit. It returns 0 for green, 1 for failed or unknown, 3 for pending or absent, and 2 on argument errors from the parser. `pr check` checks a draft.
- **Existing.** `agent wait` blocks, reading the remote run about every 30 seconds. It returns the run result and uses exit 75 after 240 seconds of continuous transport failure. It has no caller-supplied deadline for the entire wait. See `factory/cli/src/tbhbdev/alpha/agent/driver/lifecycle.py`.
- **Existing.** Main still registers three separate guards. The guard code is already split into modules under `factory/cli/src/tbhbdev/alpha/guard/`: `shell_commands.py` (315 raw lines), `gh_options.py` (21), `options.py` (54), `gh_poll/decision.py` (271), `gh_poll/token.py` (154), `gh_poll/command.py` (22), `gh_pr/decision.py` (242), `gh_pr/command.py` (29), and `gh_config_dir.py` (142). The config guard also rejects overrides of the assigned directory. See [poll guard](../reference/alpha/commands/guard-gh-poll.md), [PR guard](../reference/alpha/commands/guard-gh-pr.md), and [config guard](../reference/alpha/commands/guard-gh-config-dir.md).
- **Existing.** The [polling reference](../reference/alpha/github-polling.md) documents how the guard handles `gh query`. In the review environment, `gh query --help` exited 1 with `unknown command "query" for "gh"`. Both `gh alias list` and `gh extension list` exited 0 with no output. These checks established neither the command's definition nor its handling of non-GET methods.
- **Existing.** `alpha gh-app` can narrow installation tokens with `--permissions NAME:read`. The review set and private config handoff are in `agent/credentials.py`. The [shim](../reference/alpha/commands/gh-shim.md) keeps a count for the past hour and a core quota floor for agents and passes through outside agent sessions.
- **Existing.** `factory/cli/src/tbhbdev/telemetry.py` preserves command behavior when telemetry export fails. [GitHub's REST guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#make-requests-that-can-be-cached) documents the authorized 304 quota exemption.
- **New.** Command interfaces, policy, budgets, and measurement targets in the main text are design choices. See [Guard](#guard) for the proposed change to the poll guard's standing rule for bounded wait fallback.

### Call analysis

The operator's call analysis of 2026-11-27 audited calls and grouped them by command pattern. Clustering counted 43,389 calls, with 35,712 interactive (82%) and 7,677 headless (18%). Of the 43,446 audit calls, 43,209 were reads and 237 were writes.

A simpler interactive count differed by about 40% but ranked the commands in the same order. Of the observed calls, 78% went through `gh query` and 89% read PRs. Clusters one through eleven cover 95.5% of calls, and defaults plus flags fit an estimated 87% after weighting by cluster size.

| Rank | Command | Calls | Share | Cumulative | Field fit |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | `pr state` | 17,275 | 39.8% | 39.8% | 46% default, 88% with flags |
| 2 | `pr checks` | 7,510 | 17.3% | 57.1% | 96% |
| 3 | `pr files` | 4,479 | 10.3% | 67.5% | 98% with patch |
| 4 | `pr commits` | 2,922 | 6.7% | 74.2% | 93% |
| 5 | `pr reviews` | 2,535 | 5.8% | 80.0% | 99% |
| 6 | `repo info` | 1,994 | 4.6% | 84.6% | 76% |
| 7 | `repo file` | 1,329 | 3.1% | 87.7% | 98% |
| 8 | `commit show` | 1,194 | 2.8% | 90.4% | 87% |
| 9 | `run log` | 806 | 1.9% | 92.3% | Raw text |
| 10 | `run jobs` | 793 | 1.8% | 94.1% | 80% |
| 11 | `pr list` | 600 | 1.4% | 95.5% | 87% |
| 12 | `run list` | 365 | 0.8% | 96.4% | 77% |

In interactive sessions, 5,051 of 27,353 shell commands contained more than one call. They account for 37% of interactive calls. State appeared with checks in 1,993 commands, commits in 1,127, files in 1,034, and reviews in 808. Commits and files appeared together in 752, checks and commits in 721, at least three of the five in 1,068, and a repeated group in 803.

Across 13 groups, the long tail has 1,953 calls (4.5%). Among them are writes and downloads, plus auth status (152), release reads (139), comments (112), GraphQL (106), and search (99). Within the top eleven groups, some 3,600 calls (9%) request one-off formats and need field selection or the escape hatch.

For interactive sessions, the audit summary reports about 907 refusals, of which 802 name the poll guard. Its table gives 835 for that guard and sums to 975 in total. The audit left the cause unexplored. It notes that each GitHub call in a shell command can inherit the same outcome.

Polling denial text matches the source and directs callers to awrel. Other reported refusals came from auto-mode classification (70), an exit-status echo (31), a preceding sleep (26), and the config guard (4). Common refused shapes were single PR reads (232), commit check runs (116), run list (45 of 118), and paginated PR files (22 of 114).

### Command fields and refusal hints

Use the `tbhbdev alpha read` prefix for the commands below. PR reads other than `list` accept the common selector. Flags add fields or filter rows, and `--fields` can select any documented field.

| Detected read | Replacement | Default fields and options |
| --- | --- | --- |
| PR detail | `pr state [<pr\|sha>]` | `head_sha,state,draft,merge_state,author,title`. Add `merged,merged_at,merge_commit,merged_by` with `--merge`, `changed_files,additions,deletions,commits` with `--size`, or `body` with `--body`. |
| PR detail plus checks | `pr show [<pr\|sha>]` | State and checks. Accept state flags and `--files`, `--commits`, `--reviews`. Qualify `--fields` with the section, such as `state.head_sha`. |
| Commit checks or statuses | `pr checks [<pr\|sha>]` | `head_sha,name,status,conclusion`. Select `id,completed_at` with `--fields`. Filter with `--name <name>` or `--non-green`, which leaves the exit code for the whole head intact. |
| PR files | `pr files [<pr\|sha>]` | `filename,status,additions,deletions`. Add `patch` with `--patch`. Repeat `--exclude <glob>` or set `--max-changes <n>`. Page through the full view and report upstream caps. |
| PR commits | `pr commits [<pr\|sha>]` | `sha,signature_valid,subject,parent_count,author_email`. Select `message,parents,trailers` with `--fields` for merge and trailer checks. |
| PR reviews | `pr reviews [<pr\|sha>]` | `state,commit_id,reviewer,id`. Use `--approved-head` to show current-head approvals after removing dismissals and decisions the reviewer superseded. |
| Repository details or branch rules | `repo info [<repo>]` | `default_branch,visibility,head_sha`. Use `--branch <ref>` to choose the branch and `--rules` to add `required_checks,review_count` from applicable rules. Mark inaccessible rules absent. |
| File contents or recursive tree | `repo file <repo> <path>` | `path,sha,content`. Use `--ref <ref>` for a revision or `--tree` for recursive `path,type,sha` records. Decode API content and preserve binary bytes with `--raw`. |
| Commit, compare, or branch head | `commit show <ref>` | `sha,subject,parent_count`. Use `--compare <base>` for `base_sha,sha,status,ahead,behind`, or `--fields sha` to resolve a ref. |
| Run or job logs | `run log <id>` | `line` records from a run archive, or a job with `--job`. Use `--failed` to select failed jobs and `--raw` for plain text. |
| Jobs for a run | `run jobs <run>` | `id,name,status,conclusion,current_step`. Select `steps` with `--fields`. |
| PR list | `pr list` | `number,title,url,head_sha`. Use `--head <branch>`, `--state open\|closed\|all`, and `--limit <n>`. Default to open. |

The most requested state field is the head commit (63%). Other requests include state (37%), draft (25%), title (21%), merge state (20%), merged (19%), body (18%), changed files (14%), author (12%), and additions (11%). Head and body alone each account for 9%.

Merge, size, and body flags raise cumulative field fit from 46% to 61%, 71%, and 88%.

Check reads commonly select name (70%), status (59%), conclusion (58%), and completion time (15%). File reads select filename (63%), patch (57%), and additions and deletions (33%). Commit reads select commit ID (76%), signature verification (69%), message (53%), parents (52%), and author email (33%). Some 65% compute a parent count or check a trailer.

Review reads select state (89%), commit (78%), reviewer (75%), and ID (67%). Default branch leads repo info (51%), and 792 repo file calls specify a ref. Commit calls read one commit (50%) or compare refs (41%), with 16% resolving a branch head. Job queries select the active step, and PR lists select number (63%), title (31%), URL (26%), and head commit ID (23%).

### Output and token estimates

| Command | Example | Estimated tokens, compact vs. raw |
| --- | --- | ---: |
| `pr state <pr>` | `<sha>\topen\tfalse\tclean\t<author>\t<title>` | 10 vs. 5,526 |
| `pr checks <sha>` | `<sha>\t<check>\tcompleted\tsuccess` | 8 vs. 638 |
| `pr files <pr> --patch` | `<path>\tmodified\t2\t1\t<patch>` | 7 vs. 140 |
| `pr commits <pr>` | `<sha>\ttrue\t<subject>\t1\t<email>` | 8 vs. 760 |
| `pr reviews <pr>` | `APPROVED\t<sha>\t<reviewer>\t<id>` | 8 vs. 368 |

Divide bytes by four and round up. Count each `\t` as one tab and include the newline. Raw counts use minified sample responses of 22,104, 2,550, 557, 3,038, and 1,472 bytes in table order. [GitHub REST API description](https://docs.github.com/en/rest/about-the-rest-api/about-the-openapi-description-for-the-rest-api)

The checks sample shows one check run and no commit statuses. Real commit IDs and patches take more space, and existing `--jq` calls trim much of the raw output.

### Sanitized examples

These examples show recorded command forms, including forms that the poll guard refuses.

```bash
gh query repos/<owner>/<repo>/pulls/<n> --jq '[.head.sha,.state,.draft,.mergeable_state,.user.login,.title]|@tsv'
gh query 'repos/<owner>/<repo>/commits/<sha>/check-runs?per_page=100' --jq '.check_runs[]|select(.status != "completed" or .conclusion != "success")|[.id,.name,.status,.conclusion]|@tsv'
gh query repos/<owner>/<repo>/pulls/<n>/files --paginate --jq '.[]|[.filename,.status,.additions,.deletions,.patch]|@tsv'
gh repo view --json defaultBranchRef --jq .defaultBranchRef.name
gh run view <run-id> -R <owner>/<repo> --job <job-id> --log-failed
```
