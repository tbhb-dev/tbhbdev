# Reconciler and backfill design

Status: proposal, not reviewed. Source: `origin/main` at `96cfe1bb`. Every `file:line` below refers to that commit. Where the architecture pages and the code disagree, this design follows the code, because pages 3, 5 and 6 still call the store and the worker not yet built (`docs/architecture/05-building-block-view.md:11`, `:19`), while `internal/store/store.go` and `internal/worker/worker.go` exist.

## 1. Goal and scope

D7 makes reconciliation the only healer: a 15-minute incremental run and a nightly pass over open PRs repair missed deliveries, with no redelivery API (`docs/architecture/decisions/0007-reconciliation-is-the-only-healer.md:4`). This design covers the reconciler and the backfill that repair the pull request metrics the service emits today, using the installation token source in `internal/appauth`.

The service emits two repairable PR metrics today, both from `pull_request` `closed` webhook bodies: the `github.pr.merges` counter and the `github.pr.cycle_time` histogram, each with a `repository` attribute and the histogram adding `state` and `start_source` (`internal/recorder/recorder.go:127-133`, `:177-195`). The repair must produce exactly these series with the same `cycleStart` logic (`internal/recorder/recorder.go:197-222`).

Out of scope, because REST can't rebuild them or D11 excludes them: `github.events` counts deliveries, not PR state (`internal/recorder/recorder.go:122-125`), and `github.check.conclusions` and `github.main.red` come from `check_run` bodies (`internal/recorder/recorder.go:135-137`, `docs/architecture/decisions/0011-checks-issue-comments-pushes-and-review-threads.md:4`). The appendix A catalog (time to first review, approval, open-PR gauges, spans) isn't built, so this design leaves a seam for it and builds none of it (`docs/architecture/appendix-a-trace-and-metric-catalog.md:79-117`).

## 2. What exists

| Piece | Where | What it gives the reconciler |
| --- | --- | --- |
| Token source | `internal/appauth/appauth.go:166-184` | `Token` caches until 5 minutes remain, `Renew` always fetches. It satisfies `ghclient.TokenSource` (`internal/ghclient/ghclient.go:72-78`) |
| GitHub client | `internal/ghclient/ghclient.go:184-229` | Budget wait per send, `If-None-Match`, one `Renew` after a 401 (`:214-215`), primary and secondary backoff, `Link` paging (`:491-519`) |
| Request budget | `internal/budget/budget.go:23-28` | Priorities `Incremental`, `Sweep` and `Backfill` already exist below `Webhook` |
| PR lister | `internal/backfill/backfill.go:59-105` | Created-order list of all PRs with a page cap and a resume URL |
| Store | `internal/store/store.go:81-100` | Queue, body-hash dedupe, and `Created()`. No milestones, watermarks or tags yet (`:8-11`) |
| Worker | `internal/worker/worker.go:43-89` | Polls the queue every second (`cmd/github-metrics/main.go:43`), removes an entry only after a successful record |
| Telemetry | `internal/telemetry/telemetry.go:26-47` | A cumulative meter with a 60-second reader. No traces, no log export |

## 3. What is missing

1. A per-PR milestone record. Dedupe today is by body hash only (`internal/store/store.go:197-209`), which a REST-derived repair can never match. Without a shared key, a repair after a delivery that did arrive double-counts a merge. This blocks everything else.
2. A single recording function that both the webhook path and the repair path call. Today `Record` parses the webhook body and records inline (`internal/recorder/recorder.go:90-140`), and a reopened PR that closes twice records cycle time twice, against appendix A (`docs/architecture/appendix-a-trace-and-metric-catalog.md:135`).
3. Wiring in `main`. `main` builds no token source, budget or GitHub client, and never reads `GITHUB_APP_KEY_FILE` (`cmd/github-metrics/main.go:76-150`), though Compose mounts it (`deploy/compose.yaml:56`).
4. An updated-order lister. `backfill.List` hardcodes `sort=created&direction=asc` and `budget.Backfill` (`internal/backfill/backfill.go:66-69`), and `Record` lacks `node_id`, `updated_at`, `body` and the repository (`internal/backfill/backfill.go:27-36`).
5. A way to clear a stored `ETag`. The client stores a tag on receipt, in memory only (`internal/ghclient/ghclient.go:332-343`), and `ETags` has only `Get` and `Set` (`:103-124`). `Set(url, "")` works as a clear, because `send` skips an empty tag (`:293`), but nothing names it.
6. Two token mismatches with the docs: the access-token request has no body, so the token isn't narrowed to metadata and pull requests (`internal/appauth/appauth.go:226`, against `docs/architecture/appendix-b-app-permissions-and-events.md:109`), and `iss` is the numeric App ID (`internal/appauth/appauth.go:189-192`) where appendix B says the client ID (`docs/architecture/appendix-b-app-permissions-and-events.md:107`). GitHub accepts either as `iss` (unverified here).

## 4. GitHub calls and their cost

Checked with `gh` against this public repository on 2026-11-22: an item of `GET /repos/{owner}/{repo}/pulls` carries `node_id`, `created_at`, `updated_at`, `closed_at`, `merged_at`, `body`, `user.type` and `base`, and has no `merged` field, while `GET /repos/{owner}/{repo}/pulls/{n}` has `merged`. So the repair derives merged as `merged_at != null` and needs list calls only, with no per-PR fetch for today's two metrics.

| Call | Priority | When | Requests |
| --- | --- | --- | --- |
| `GET /installation/repositories?per_page=100` | `Incremental` | Start of every run | 1 per 100 repositories |
| `GET /repos/{o}/{r}/pulls?state=all&sort=updated&direction=desc&per_page=100` | `Incremental` or `Sweep` | Per repository, until the stop time | Usually 1, capped at 50 pages |
| `GET /repos/{o}/{r}/pulls?state=all&sort=created&direction=asc&per_page=100` | `Backfill` | Backfill only, through `backfill.List` | 1 per 100 PRs |
| `GET /repos/{o}/{r}/issues/{n}/events?per_page=100` | `Incremental` or `Sweep` | Once per repair candidate that would record `ended` | Rare: only for PRs the webhook missed |
| `POST /app/installations/{id}/access_tokens` | none, outside the budget | About every 55 minutes, and once per renewal after a 401 | 1 |

With about 50 repositories, one run costs about 51 requests, so 4 runs an hour cost about 204 for one-page lists with no `304` and no repairs. That figure is a baseline: a truncated repository adds up to 49 more pages to an incremental walk and up to 199 more to the next sweep, and each repair candidate adds up to 3 event calls, so a bad hour costs more and the budget's floor stop at 500 remaining is what bounds it. The baseline is close to appendix E's 180 for the incremental line (`docs/architecture/appendix-e-budget-dedupe-and-retention.md:17`). A `304` with an `Authorization` header doesn't count against the primary limit (`docs/architecture/appendix-c-github-limit-evidence.md:39`), but it still takes a bucket token (`docs/architecture/appendix-e-budget-dedupe-and-retention.md:26`). Whether the token exchange counts against the installation's REST limit is unverified. The budget's floor stop at 500 remaining covers other use of the limit (`internal/budget/budget.go:136-149`).

Token plug-in: `main` builds one `*appauth.Source` and passes it as the `ghclient.TokenSource`. The client calls `Token` before each send and `Renew` once after a 401, and a second 401 returns a `*StatusError` (`internal/ghclient/ghclient.go:214-224`). A source failure surfaces as `ErrTokenSource` with no source text (`internal/ghclient/ghclient.go:263-280`), and the source logs its own stage and status (`internal/appauth/appauth.go:211-215`). If a refresh fails, `Token` returns the cached token while it is still valid and clears it once it expires. `Renew` reuses a valid token another caller fetched while it waited for the mutex. Two callers that both see a 401 usually share one exchange.

## 5. The idempotency key

The store gains a `milestones` bucket. Its key is the PR `node_id`, a zero byte, and the milestone name, `merged` or `ended`. Its value is the commit time. `ClaimMilestone(key) (claimed bool, err error)` runs one synced `bbolt` update, like `Enqueue` (`internal/store/store.go:190-216`): it returns `true` and writes the key when absent, and `false` when present.

`recorder` gains `RecordClosed(ctx, ClosedPR) (Outcome, error)`. `ClosedPR` holds the repository full name, `node_id`, `created_at`, `merged_at`, the merged flag, `ended_at`, the ended state and the body, which is read for `started_at` and never stored, as today (`internal/recorder/recorder.go:197-222`). The `ended` milestone reads only `ended_at` and the ended state, which is `merged` or `closed` and picks the `state` series of `github.pr.cycle_time`, and the `merged` milestone reads only `merged_at`, so the first end's time and state travel together and don't depend on a later merge. The webhook path sets `ended_at` from `closed_at` and the ended state from the merged flag of that delivery. For each milestone it follows appendix A's rule (`docs/architecture/appendix-a-trace-and-metric-catalog.md:168-176`):

1. If the end time is 24 hours old or more, or not after the store's creation time, it claims and records nothing, as appendix A step 5 says. Outcome `too_old`. A repair also requires the end time to be after the repair floor (below). A repair whose first end is inside the 24-hour window but before the floor claims `ended` and records nothing, outcome `pre_floor`. The old recorder may already have counted that close with no claim, and a merge delivery after a reopen would count the PR's cycle time a second time. Appendix A accepts a missed count over a double count, so the repair holds the claim. The webhook path ignores the repair floor, so a delivery queued before a deploy and processed after it is still counted. Residual risk: a first close accepted before the floor whose delivery is still queued or retrying when a repair or merge delivery claims `ended` loses that one observation, and a late first-close delivery after a merge delivery can leave one observation at the merge time. A close, reopen and merge inside one second can also put the first close in `state=merged`. All three are for the implementation's tests.
2. Otherwise it claims. A `false` claim means another path already counted it. Outcome `already_counted`.
3. Only after a `true` claim does it record: `merged` adds 1 to `github.pr.merges`, and `ended` records `github.pr.cycle_time`. Outcome `recorded`.
4. A claim error records nothing and returns the error, so the caller retries on the next pass.

The recorder reads the creation time, the repair floor and the claim through one `Ledger` interface that the store satisfies, and takes its clock as a field that defaults to `time.Now`. The webhook `Record` keeps parsing the body, now also reads `pull_request.node_id`, builds a `ClosedPR`, and calls `RecordClosed` before any other `Add`. Today `Record` adds to `github.events` first (`internal/recorder/recorder.go:122-125`), and the worker retries an entry whose record failed (`internal/worker/worker.go:75-79`), so a claim error after that `Add` would count the delivery again on every retry. A body with no `node_id` counts under the existing `dropped` counter with a new reason, `no_node_id`, and leaves the queue, since the reconciler will repair it. The repair path builds the same `ClosedPR` from a list item. Both paths share one key, so whichever runs first counts and the other sees `already_counted`. A replayed old body, or a webhook arriving after the repair, never double-counts.

Upgrade window: PRs the webhook path counted before it began claiming have no claim, so a repair of one that ended in the last 24 hours would count it again. The store therefore keeps a `repair_floor` meta key, stamped with the time `main` first builds the claiming recorder and returned unchanged after that. Only the repair path reads it. That gives up repairing misses from the 24 hours before the upgrade, and never double-counts. A rollback below B puts the base recorder back, which counts merges with no claim. The guard is an operator flag: starting with `RECONCILE_FLOOR_RESET=1` stamps `repair_floor` to the start time once and logs the new floor, and the deploy runbook says to set it for the first start after any rollback below B. The service also logs the current floor at every start. Residual risk: a rollback with no reset can double-count merges from the rollback window, because the base binary leaves no trace the new one can read.

A crash between the claim and the record loses one observation. Appendix A accepts that over a double count (`docs/architecture/appendix-a-trace-and-metric-catalog.md:176`). A repaired observation lands at repair time, up to about 15 minutes after the merge, because a cumulative histogram has no backdated timestamp. The value is still the true duration.

## 6. Loop shape

A new package, `internal/reconcile`, runs one goroutine from `main` beside the worker (`cmd/github-metrics/main.go:124-130`).

- Schedule: an incremental run at startup and then every 15 minutes. A sweep once a day at 03:00 UTC. A `time.Ticker` drops ticks while a run is busy, so runs never overlap, and the sweep waits for a running incremental run to finish.
- Run bound: each incremental run gets a context with a 14-minute deadline to keep it from overlapping the next tick. A sweep gets a 45-minute deadline to read the full repair window at the shared request rate.
- Repository list: page through `/installation/repositories`, capped at 10 pages. The client stores a tag on receipt (`internal/ghclient/ghclient.go:332-343`), and a page-one tag can't see a change confined to page two. So the reconciler sends the tag only when the last full list fit on one page and every page succeeded. For a longer list, or after any failed page, it calls `etags.Set(url, "")` and reads every page in full, at one request per 100 repositories. A `304` then reuses the cached list only in the one-page case. The response shape of that endpoint, and whether a narrowed token may call it, are unverified. Each repository is processed in turn, with no concurrency, since the budget paces every request anyway (`internal/budget/budget.go:36-38`).
- Incremental walk: list `sort=updated&direction=desc` at `budget.Incremental`. Stop at the first item whose `updated_at` is before the stop time, which is the later of the watermark minus a 5-minute overlap and now minus 24 hours. Older milestones can't be recorded anyway, so the 24-hour floor bounds a first run and a run after an outage. Stop after 50 pages (5,000 PRs updated inside the window, far past this installation's scale) and mark the repository truncated: the watermark doesn't advance, the run logs the repository and counts `truncated`, and the next sweep walks that repository with a 200-page cap. Because each repair is idempotent through the claim, a repeated walk costs requests and never double-counts.
- The walk relies on a close or a merge moving `updated_at`, which GitHub's sort order implies but nobody here has verified.
- Per item: skip an item whose `closed_at` is null. Otherwise build a `ClosedPR`. REST can't tell a first close from a later one, and appendix A says the first end wins (`docs/architecture/appendix-a-trace-and-metric-catalog.md:135`). So before a repair would record `ended`, it reads the PR's issue events (`GET /repos/{o}/{r}/issues/{n}/events`, up to 3 pages of 100, one call per page, at the priority of the walk: `budget.Incremental` in an incremental run and `budget.Sweep` in a sweep) and takes the `created_at` of the earliest `closed` event as the first end time, whatever later reopens and closes follow. The repair sets `ended_at` to that time and the ended state to `merged` when a `merged` event shares its `created_at`, and to `closed` otherwise. That time goes through the same gates as any other. The repair records `merged` from `merged_at`, which is the single merge, and `ended` at the first close in its own state, so a PR that closed, reopened and merged counts its cycle time at the first close with `state=closed`, and a later webhook delivery of either the close or the merge returns `already_counted`. If the events run past 3 pages, or hold no `closed` event, the repair records `merged` only, leaves `ended` unclaimed with outcome `ambiguous`, and the PR is read again whenever the walk reaches it, which happens when it is updated again or while a sweep covers its 24-hour window. Then call `RecordClosed`. Limitation: a PR closed and reopened between two runs, with its close delivery missed, shows a null `closed_at` and its `ended` milestone is never repaired.
- Watermark: a `watermarks` bucket keyed by repository full name holds the newest `updated_at` seen. It advances only after the walk reached the stop time and every item returned without an error.
- `ETag` use: only the first page sends a tag. A `304` there means the repository is unchanged since the last clean walk in this process, so it's done. Before each later page the reconciler calls `etags.Set(url, "")`, because page contents shift as PRs update, so a later page never answers `304`.
- `ETag` clear: whenever a repository's walk doesn't finish cleanly (truncated, deadline, `ErrGaveUp`, a decode or store error), the reconciler clears the first-page tag with `etags.Set(url, "")`, so the next run reads the page in full instead of getting a `304` that hides the gap (`docs/architecture/11-risks-and-technical-debt.md:11`). Tags stay in memory as today. Moving them into the store, as D4 lists, is later work.
- Sweep: the same walk at `budget.Sweep`, ignoring the watermark and using the 24-hour floor alone. It clears each first-page tag before it starts, since it uses the same URL as the incremental walk and would otherwise get a `304`. It repairs a watermark that moved past a miss. D7's pass over open PRs repairs open-PR state, which doesn't exist yet, so it lands with the open-PR table.
- Disabled cases: the reconciler doesn't start when the App ID, the installation ID or the key file is unset, and `main` logs one line saying why. The recorder choice stays as today: `noopRecorder` only when no collector endpoint is set (`cmd/github-metrics/main.go:152-168`), and the claiming recorder in every other case, so a webhook-only deployment keeps exporting and its queue drains. With no collector endpoint the recorder errors, so a claim followed by a failed record can't happen, because `RecordClosed` is never reached.

New environment variables, following page 7's list (`docs/architecture/07-deployment-view.md:11`): `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_API_URL` (default `https://api.github.com`), and the existing `GITHUB_APP_KEY_FILE`. The key stays a file, never an environment value (`docs/architecture/02-constraints.md:12`).

## 7. Backfill

Under the 24-hour and creation-time gates, a backfill over history records no metric observation. Every milestone in it is older than the store, and no trace export exists yet (`internal/telemetry/telemetry.go:4`). The glossary already says a backfill produces traces and no metrics (`docs/architecture/12-glossary.md:8`). A backfill claims a milestone only when it fails those gates and skips every milestone that passes them, leaving it to the webhook path and the reconciler, so a PR that merges while a backfill runs can't lose its count. So today a backfill would write only `too_old` claims, which mark milestones as seen with no observation (`docs/architecture/appendix-e-budget-dedupe-and-retention.md:40`), and a resume cursor. Those claims change nothing a later run would do, since the same gates already block those milestones.

`backfill.List` takes no resume input today, since it always builds page one (`internal/backfill/backfill.go:21-24`, `:59-75`), so the job needs a `Start` field first. A backfill of a repository added to an existing store can also meet milestones that pass both time gates, so the job leaves them unclaimed and records neither a claim nor a metric for them, and claims only milestones that fail the gates. A trace for a live milestone needs its own `traced` marker, separate from the claim, which the trace work adds. When traces or the open-PR table land, the backfill becomes a resumable job: one repository at a time through `backfill.List` at `budget.Backfill`, with `MaxRequests` per step and `ResumeURL` saved in a `backfill_cursor` bucket after each step, so a restart resumes. It starts when the store is new, and on an `installation_repositories` body that lists `repositories_added` (`docs/architecture/06-runtime-view.md:10`). The saved resume URL is absolute, and the client rejects one on another host (`internal/ghclient/ghclient.go:249-260`). `Record` needs `node_id` and `updated_at` added for that work. This design recommends deferring the job until then (open question 2).

## 8. Failure behavior

| Failure | Behavior |
| --- | --- |
| `ErrTokenSource`, or a `*StatusError` with 401 after the renewal | End the run with result `auth_failed`. Advance no watermark past what was committed. Retry on the next tick |
| `*StatusError` 403 or 404 for one repository | Log it, skip the repository, finish the run with result `partial` |
| `ErrGaveUp`, or the run deadline | Stop the run with result `partial`. The budget has already backed off (`internal/ghclient/ghclient.go:216-222`) |
| `ErrBodyTooLarge`, or a decode error | Skip the repository, clear its first-page tag, result `partial` |
| A store error from a claim or a watermark write | Clear the first-page tag and return the error. Count the pass as `partial` and retry on the next tick. After three consecutive pass errors, return the last error to stop the service. The restart policy brings it back (`deploy/compose.yaml:51`) |
| Collector down | Records go into cumulative memory and leave at the next accepted export. A crash during the outage loses them, as appendix A accepts (`docs/architecture/appendix-a-trace-and-metric-catalog.md:186-190`) |

## 9. What the service reports about itself

Metrics, on the meter from `telemetry.Setup`, with no repository attribute so the reconciler adds at most a few dozen series:

| Instrument | Kind | Attributes | Meaning |
| --- | --- | --- | --- |
| `github_metrics.reconcile.runs` | Counter | `kind` (`incremental`, `sweep`), `result` (`ok`, `partial`, `auth_failed`) | One per finished run |
| `github_metrics.reconcile.duration` | Histogram, `s` | `kind` | Run wall time |
| `github_metrics.reconcile.repairs` | Counter | `kind`, `milestone` (`merged`, `ended`) | `RecordClosed` returned `recorded` on the repair path, meaning the webhook path missed it or hadn't run yet |
| `github_metrics.reconcile.requests` | Counter | `kind`, `status` (`ok`, `not_modified`, `error`) | Responses the reconciler received |
| `github_metrics.reconcile.last_success` | Gauge, `s` | `kind` | Unix time of the last `ok` run, for a staleness alert and the planned `/readyz` (`docs/architecture/07-deployment-view.md:12`) |

`repairs` runs a little above the true miss count: a delivery still in the queue when the run reaches its PR loses the race. The worker polls every second, so the window is small.

The existing JSON handler (`cmd/github-metrics/main.go:67`) writes one summary per run with `kind`, `result`, `repos`, `pages`, `not_modified`, `recorded`, `already_counted`, `truncated` and `duration`. A run that returns an error logs its summary at error level and omits the error text. Other run summaries log at info level. Each skipped or truncated repository gets a warning with its numeric repository ID and status. An `auth_failed` result gets an error line alongside the token source's own line. No line carries a repository name, a token, a body, a title or a PR number.

## 10. Pull request split

Line counts are estimates of added Go lines, tests included, each under 400.

| PR | Content | Files | Estimate | Depends on |
| --- | --- | --- | --- | --- |
| A | `milestones` bucket, `ClaimMilestone`, `RepairFloor`, watermark get and set | `internal/store/store.go`, `internal/store/store_test.go` | 250 | none |
| B | `ClosedPR`, `RecordClosed` and the eligibility rule behind the `Ledger` interface; webhook `Record` claims before any `Add`; `no_node_id` drop reason; `recorder.New(meter, ledger)` and `main` passes the store, so the webhook path claims from the release that ships B | `internal/recorder/recorder.go`, `internal/recorder/recorder_test.go`, `cmd/github-metrics/main.go`, `cmd/github-metrics/main_test.go` | 360 | A to merge; can be written in parallel against a fake `Ledger` |
| C | Narrow the token request to metadata and pull requests read | `internal/appauth/appauth.go`, `internal/appauth/appauth_test.go` | 80 | none |
| D | Updated-order lister: repository list with its cache, the bounded walk, the later-page tag clear, returning `ClosedPR` values and the newest `updated_at` | `internal/reconcile/list.go`, `internal/reconcile/list_test.go`, `internal/reconcile/BUILD.bazel` | 320 | none |
| E | One run: per-repository walk, `RecordClosed` calls, watermark advance, first-page tag clear, the failure table as a run result | `internal/reconcile/run.go`, `internal/reconcile/run_test.go`, `internal/reconcile/BUILD.bazel` | 320 | D; A and B through interfaces |
| F | Scheduler: startup run, 15-minute ticker, deadline, self-metrics and run logs | `internal/reconcile/loop.go`, `internal/reconcile/loop_test.go`, `internal/reconcile/BUILD.bazel` | 250 | E |
| G | Wiring: read the App config, build the source, budget and client, start the reconciler, disabled cases; Compose environment; pages 5 and 6 status; the `repair_floor` stamp, the `RECONCILE_FLOOR_RESET` flag and the rollback rule in the deploy notes | `cmd/github-metrics/main.go`, `cmd/github-metrics/main_test.go`, `deploy/compose.yaml`, `deploy/deploy_test.go`, `docs/architecture/05-building-block-view.md`, `docs/architecture/06-runtime-view.md` | 220 | B deployed, C merged, F |
| H | Daily sweep at `budget.Sweep` | `internal/reconcile/loop.go`, `internal/reconcile/loop_test.go` | 120 | F |
| I | Backfill job and cursor, if open question 2 says now | `internal/backfill/backfill.go`, `internal/backfill/backfill_test.go`, `internal/store/store.go`, `internal/store/store_test.go`, `cmd/github-metrics/main.go` | 350 | A, G |

A, C and D can be built and merged in parallel, and B can be written alongside them and merge right after A. E starts once D merges, since both touch `internal/reconcile/BUILD.bazel`. B must merge and deploy before G turns the reconciler on, and C must merge first so the token request is already narrowed, because the webhook path has to claim first, or a repair after a delivery that did arrive double-counts. Because `RepairFloor` is first called by B's wiring, A alone never stamps the cut-over. `bazelisk run //:gazelle` regenerates the `BUILD.bazel` files in D, E and F (`AGENTS.md:23`).

## 11. Open questions for a human

1. Metric names: the code emits `github.pr.merges` and `github.pr.cycle_time` (`internal/recorder/recorder.go:52-53`, `:72`) with a `repository` attribute, while appendix A and D10 name `github.pull_request.merged` and `github.pull_request.cycle_time` with `vcs.repository.name` and `github.author.type` (`docs/architecture/appendix-a-trace-and-metric-catalog.md:105`, `:131`, `:139`). Does the repair keep today's names, or does a rename come first? A rename starts new series (`docs/architecture/decisions/0010-repository-and-author-type-are-the-only-metric-a.md:5`).
2. Backfill now or later: it records no observation until traces or the open-PR table exist. Build only the cursor now, or defer PR I?
3. Webhook bodies as a metric source: appendix A says telemetry content comes only from REST, never from a webhook body (`docs/architecture/appendix-a-trace-and-metric-catalog.md:52`), but the recorder reads closed bodies today. This design keeps that behind the shared claim. Should the webhook path instead only trigger a REST reconcile, as page 4 plans (`docs/architecture/04-solution-strategy.md:7`)?
4. The upgrade cut-over gives up repairing misses from the 24 hours before the upgrade. Acceptable?
5. Self-metric naming and attributes: is a `github_metrics.reconcile.` prefix right, and should any of them carry the repository despite D10, which covers PR metrics?
6. The sweep hour (03:00 UTC proposed) and the 50-page and 10-page caps: fixed constants, or operator settings?
7. Token narrowing (PR C) and `iss` as App ID versus client ID: fix the code, or correct appendix B?
