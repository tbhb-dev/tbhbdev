# Reconciler and backfill design

Status: proposal, not reviewed. The `file:line` references and source line numbers refer to `origin/main` at `96cfe1bb`. Where the architecture pages and the code disagree, this design follows the code. Pages 3, 5, and 6 still describe the store and worker as future work (`docs/architecture/05-building-block-view.md:11`, `:19`), while `internal/store/store.go` and `internal/worker/worker.go` exist.

## 1. Goal and scope

Under D7, reconciliation repairs missed deliveries without calling the redelivery API. It runs incrementally every 15 minutes and checks open PRs each night (`docs/architecture/decisions/0007-reconciliation-is-the-only-healer.md:4`). This design covers reconciliation and backfill for the pull request metrics the service emits today. Both use the [installation token source](../../internal/appauth).

REST can repair two PR metrics that the service records today from `pull_request` `closed` webhook bodies. Both the `github.pr.merges` counter and the `github.pr.cycle_time` histogram have a `repository` attribute. For the histogram, also include `state` and `start_source` (`internal/recorder/recorder.go:127-133`, `:177-195`). Repairs must reproduce these series using the same `cycleStart` logic (`internal/recorder/recorder.go:197-222`).

The other metrics fall outside this design because REST can’t rebuild them or D11 excludes them. Delivery counts come from `github.events` (`internal/recorder/recorder.go:122-125`). From `check_run` bodies the service gets `github.check.conclusions` and `github.main.red` (`internal/recorder/recorder.go:135-137`, `docs/architecture/decisions/0011-checks-issue-comments-pushes-and-review-threads.md:4`). Appendix A’s time to first review, time to approval, open-PR gauges, and spans remain future work. This design leaves room to add them (`docs/architecture/appendix-a-trace-and-metric-catalog.md:79-117`).

## 2. What exists

| Piece | Where | What it gives the reconciler |
| --- | --- | --- |
| Token source | [Token source, lines 166 to 184](../../internal/appauth/appauth.go) | `Token` caches until 5 minutes remain. `Renew` always fetches. The source satisfies the client’s [`TokenSource` interface, lines 72 to 78](../../internal/ghclient/ghclient.go) |
| GitHub client | [Send path, lines 184 to 229](../../internal/ghclient/ghclient.go) | Budget wait per send, `If-None-Match`, one `Renew` after a 401 (`:214-215`), primary and secondary backoff, `Link` paging (`:491-519`) |
| Request budget | `internal/budget/budget.go:23-28` | Priorities `Incremental`, `Sweep` and `Backfill` already exist below `Webhook` |
| PR lister | `internal/backfill/backfill.go:59-105` | Created-order list of all PRs with a page cap and a URL for continuing the list |
| Store | `internal/store/store.go:81-100` | Queue, body-hash dedupe, and `Created()`. No milestones, watermarks or tags yet (`:8-11`) |
| Worker | `internal/worker/worker.go:43-89` | Polls the queue every second (`cmd/github-metrics/main.go:43`) and waits for successful recording before removing an entry |
| Telemetry | `internal/telemetry/telemetry.go:26-47` | A cumulative meter with a 60-second reader. Trace and log export are absent |

## 3. Missing pieces

1. A per-PR milestone record. The store deduplicates by body hash (`internal/store/store.go:197-209`). A repair from REST can’t match that hash. Without a shared key, a repair after a delivery that did arrive double-counts a merge. This blocks the rest of the work.
2. A recording function shared by the webhook and repair paths. Today `Record` parses the webhook body and records inline (`internal/recorder/recorder.go:90-140`). When a reopened PR closes twice, the recorder counts its cycle time twice, against appendix A (`docs/architecture/appendix-a-trace-and-metric-catalog.md:135`).
3. Wiring in `main`. `main` builds no token source, budget or GitHub client, and never reads `GITHUB_APP_KEY_FILE` (`cmd/github-metrics/main.go:76-150`), even when the host supplies that variable.
4. An updated-order lister. `backfill.List` hardcodes `sort=created&direction=asc` and `budget.Backfill` (`internal/backfill/backfill.go:66-69`), and `Record` lacks `node_id`, `updated_at`, `body` and the repository (`internal/backfill/backfill.go:27-36`).
5. A way to clear a stored `ETag`. On receipt, the client [stores a tag in memory, lines 332 to 343](../../internal/ghclient/ghclient.go). Its [`ETags` interface, lines 103 to 124](../../internal/ghclient/ghclient.go), exposes `Get` and `Set`. `Set(url, "")` clears the tag because `send` [skips an empty tag, line 293](../../internal/ghclient/ghclient.go). Clearing a tag has no named operation.
6. The [access-token request, line 226](../../internal/appauth/appauth.go), omits a body and leaves the token permissions unrestricted by the request. Appendix B limits them to metadata and pull requests (`docs/architecture/appendix-b-app-permissions-and-events.md:109`). For the issuer, the code [sets `iss` to the numeric App ID, lines 189 to 192](../../internal/appauth/appauth.go). Appendix B specifies the client ID (`docs/architecture/appendix-b-app-permissions-and-events.md:107`). Either ID is valid as `iss` on GitHub (unverified here).

## 4. GitHub calls and their cost

On 2026-11-22, the author used `gh` to check both endpoints against this public repository. Each item from `GET /repos/{owner}/{repo}/pulls` includes `node_id`, `created_at`, `updated_at`, `closed_at`, `merged_at`, `body`, `user.type`, and `base`.

Access to the `merged` field requires `GET /repos/{owner}/{repo}/pulls/{n}`. For today’s two metrics, the repair can use list calls and derive the merged flag from `merged_at != null` without a per-PR fetch.

| Call | Priority | When | Requests |
| --- | --- | --- | --- |
| `GET /installation/repositories?per_page=100` | `Incremental` | Start of every run | 1 per 100 repositories |
| `GET /repos/{o}/{r}/pulls?state=all&sort=updated&direction=desc&per_page=100` | `Incremental` or `Sweep` | Per repository, until the stop time | 1 in the ordinary case, capped at 50 pages |
| `GET /repos/{o}/{r}/pulls?state=all&sort=created&direction=asc&per_page=100` | `Backfill` | During backfill via `backfill.List` | 1 per 100 PRs |
| `GET /repos/{o}/{r}/issues/{n}/events?per_page=100` | `Incremental` or `Sweep` | Once per repair candidate that would record `ended` | Rare, for PRs the webhook missed |
| `POST /app/installations/{id}/access_tokens` | none, outside the budget | About every 55 minutes, and once per renewal after a 401 | 1 |

For about 50 repositories, a run takes about 51 requests when lists fit on one page, none return `304`, and no PRs need repair. At four runs an hour, expect about 204 requests under those conditions, close to the 180 for incremental runs in appendix E (`docs/architecture/appendix-e-budget-dedupe-and-retention.md:17`). A truncated repository adds up to 49 pages to an incremental walk and up to 199 to the next sweep. Each repair candidate can add up to 3 event calls. To reserve capacity for other uses, the budget stops work at 500 requests remaining (`internal/budget/budget.go:136-149`). A `304` with an `Authorization` header still takes a bucket token (`docs/architecture/appendix-e-budget-dedupe-and-retention.md:26`) and doesn’t count against the primary limit (`docs/architecture/appendix-c-github-limit-evidence.md:39`).

Whether the token exchange counts against the installation’s REST limit is unverified.

`main` creates one instance of [`Source`](../../internal/appauth/appauth.go) and passes a pointer to the client as its [`TokenSource`](../../internal/ghclient/ghclient.go). Before each send the client calls `Token`. After a 401 it calls `Renew` once, and a second 401 returns a [`*StatusError`, lines 214 to 224](../../internal/ghclient/ghclient.go). When the source fails, the client returns [`ErrTokenSource` without source text, lines 263 to 280](../../internal/ghclient/ghclient.go). For a failure the source [logs its stage and status, lines 211 to 215](../../internal/appauth/appauth.go). If a refresh fails, `Token` returns a cached token while valid and clears it once it expires. When callers see the same 401, they generally share an exchange: after waiting for the mutex, `Renew` reuses a valid token another caller fetched.

## 5. The idempotency key

A new `milestones` bucket stores commit times. Build the key from the PR `node_id`, a zero byte, and the milestone name, `merged` or `ended`. `ClaimMilestone(key) (claimed bool, err error)` runs one synced store update, like `Enqueue` (`internal/store/store.go:190-216`): it returns `true` and writes the key when absent, and `false` when present.

`recorder` gains `RecordClosed(ctx, ClosedPR) (Outcome, error)`. Fields in `ClosedPR` include the repository full name, `node_id`, `created_at`, `merged_at`, the merged flag, `ended_at`, the ended state, and the body. As today, the recorder reads the body for `started_at` without storing it (`internal/recorder/recorder.go:197-222`). For `ended`, the recorder uses `ended_at` and the ended state, which picks the `merged` or `closed` series under the `state` attribute of `github.pr.cycle_time`. For the `merged` milestone it needs `merged_at` alone. A later merge leaves both the time and state of the first end unchanged. The webhook path sets `ended_at` from `closed_at` and sets the ended state from that delivery’s merged flag. For each milestone it follows appendix A (`docs/architecture/appendix-a-trace-and-metric-catalog.md:168-176`).

1. Return `too_old` without claiming or recording if the end time is at least 24 hours old or if it occurred at or before store creation, as appendix A step 5 specifies. A repair also requires an end time after the repair floor. If the first end falls within 24 hours but before the floor, the repair claims `ended` without recording and returns `pre_floor`. The old recorder may have counted that close without a claim. A merge delivery after a reopen could then count cycle time twice for that PR. Keeping the claim follows appendix A’s preference for a missed count over a duplicate. The webhook path ignores the floor and counts deliveries that entered the queue before deployment and run afterward. Test three remaining cases. If a repair or merge delivery claims `ended` while a first close accepted before the floor waits in the queue or retries, the first close loses its observation. If a first-close delivery arrives after a merge delivery, one observation may remain at the merge time. A close, reopen, and merge within one second can assign `state=merged` to the first close.
2. Otherwise it claims. A `false` claim means another path already counted it. Outcome `already_counted`.
3. Record the metric after a `true` claim. For `merged`, add 1 to `github.pr.merges`. For `ended`, record `github.pr.cycle_time`. Return `recorded`.
4. If the claim fails, return the error without recording. The caller retries on the next pass.

The store satisfies the `Ledger` interface, which gives the recorder the creation time, the repair floor, and the claim operation. Its clock field defaults to `time.Now`. On the webhook path, `Record` continues to parse the body and also reads `pull_request.node_id`. It builds a `ClosedPR` and calls `RecordClosed` before any other `Add`. Today `Record` adds to `github.events` first (`internal/recorder/recorder.go:122-125`). Because the worker retries an entry after a recording failure (`internal/worker/worker.go:75-79`), a claim error after that `Add` would count the delivery again on each retry. If a body lacks `node_id`, the recorder increments the existing `dropped` counter with reason `no_node_id`. The entry leaves the queue, and the reconciler can repair it. On the repair path, the same `ClosedPR` comes from a list item. Both paths use the same key. Whichever runs first counts, and the other returns `already_counted`. This prevents a replay or a late webhook from counting again.

Before the upgrade, the webhook path counted PRs without claiming them. A repair of one that ended within the last 24 hours could count it again. To prevent that, the store keeps the time when `main` first builds the claiming recorder under the `repair_floor` meta key and returns that value on later reads. The floor applies to repairs. Repairs skip misses from the 24 hours before the upgrade. Rolling back to a version before B restores the base recorder, which counts merges without claims. After that rollback, start with `RECONCILE_FLOOR_RESET=1` to stamp the new floor once at startup and log it. See the [deployment view](../architecture/07-deployment-view.md#health-and-restarts) for when to set the flag. At every start the service logs the current floor. Without a reset after rollback, the repair can double-count merges from the rollback window because the base binary leaves nothing for the new recorder to read.

A crash between the claim and the record loses one observation. Appendix A accepts that over a double count (`docs/architecture/appendix-a-trace-and-metric-catalog.md:176`). A repaired observation lands at repair time, up to about 15 minutes after the merge, because a cumulative histogram has no backdated timestamp. The observed value is the actual duration.

## 6. Loop shape

A new package, `internal/reconcile`, runs one goroutine from `main` beside the worker (`cmd/github-metrics/main.go:124-130`).

- **Schedule.** Run incrementally at startup and every 15 minutes. Start one sweep daily at 03:00 Coordinated Universal Time. A `time.Ticker` drops ticks while a run is busy. Runs never overlap, and the sweep waits for a running incremental pass to finish.
- Run bound: each incremental run gets a context with a 14-minute deadline to keep it from overlapping the next tick. A sweep gets a 45-minute deadline to read the full repair window at the shared request rate.
- **Repository list.** Page through `/installation/repositories` up to 10 pages. The client [stores a tag when it receives one, lines 332 to 343](../../internal/ghclient/ghclient.go). A tag for page one can’t detect a change on page two. Send a tag and reuse the cached list on a `304` if the last full list fit on one page and every request succeeded. After a longer list or any failed page, call `etags.Set(url, "")` and read every page in full at one request per 100 repositories. Nobody has verified which fields the endpoint returns or whether it accepts a narrowed token. Process repositories one at a time because the budget paces each request (`internal/budget/budget.go:36-38`).
- For the incremental walk, list `sort=updated&direction=desc` at `budget.Incremental`. Stop at the first item whose `updated_at` predates the stop time. Use the later of the watermark minus a 5-minute overlap and now minus 24 hours. The recorder skips older milestones, which limits the work on the first run and after an outage. Stop at 50 pages (5,000 PRs updated in the window, far beyond this installation’s scale) and mark the repository truncated. Keep its watermark unchanged. Log the repository, count `truncated`, and use a 200-page cap on the next sweep of that repository. Repeating the walk costs requests, and the claim prevents duplicate observations.
- The walk relies on a close or a merge moving `updated_at`, which GitHub's sort order implies but nobody here has verified.
- **Per item.** Skip an item with null `closed_at`. Otherwise build a `ClosedPR`. REST list items don’t distinguish a first close from a later one. Appendix A counts the first end (`docs/architecture/appendix-a-trace-and-metric-catalog.md:135`). Before repairing `ended`, read `GET /repos/{o}/{r}/issues/{n}/events` at the walk’s priority, either `budget.Incremental` or `budget.Sweep`. Read up to 3 pages of 100 events at one call per page. Set `ended_at` to the `created_at` of the earliest `closed` event. Ignore later closes and reopens for that timestamp. Use state `merged` if a `merged` event has the same `created_at`. Otherwise use `closed`. Apply the usual time gates. Record `merged` from `merged_at` and `ended` in the state at the first close. A PR that closes, reopens, then merges counts its cycle time at the first close with `state=closed`. A later webhook delivery of the close or merge returns `already_counted`. If there are more than 3 pages or no `closed` event, leave `ended` unclaimed with outcome `ambiguous` and proceed with `merged`. The reconciler reads the PR again after another update or during a sweep that covers its 24-hour window. Then call `RecordClosed`. If a PR closes and reopens between runs and the service misses the close delivery, the list shows null `closed_at`. In that case the reconciler skips the `ended` milestone.
- **Watermark.** The `watermarks` bucket uses the full repository name as the key and stores the newest `updated_at` seen. Advance it once the walk hits the stop time and every item has returned without error.
- **`ETag` use.** Send a tag on the first page. If it returns `304`, the repository hasn’t changed since the last clean walk in this process and needs no more requests. Page contents shift when PRs update. Before each later page, call `etags.Set(url, "")` to prevent a `304`.
- **Clearing tags.** Clear the first-page tag with `etags.Set(url, "")` if a walk truncates, hits its deadline, or encounters `ErrGaveUp`, a decode error, or a store error. The next run reads the page in full. A `304` would hide the gap (`docs/architecture/11-risks-and-technical-debt.md:11`). Tags stay in memory as today. D4 plans for the store to hold them later.
- Sweep: the same walk at `budget.Sweep`, ignoring the watermark and using the 24-hour floor alone. It clears each first-page tag before it starts, since it uses the same URL as the incremental walk and would otherwise get a `304`. It repairs a watermark that moved past a miss. D7's pass over open PRs repairs open-PR state, which doesn't exist yet, so it lands with the open-PR table.
- **Missing configuration.** The reconciler requires the App ID, installation ID, and key file. When any is missing, `main` logs the reason in one line and skips startup of the reconciler. As today, `main` selects `noopRecorder` if the configuration lacks a collector endpoint (`cmd/github-metrics/main.go:152-168`). Otherwise it selects the claiming recorder, and deployments that use webhooks without reconciliation keep exporting and draining the queue. Without an endpoint the recorder returns an error before calling `RecordClosed`, which prevents a claim followed by a failed record.

New environment variables, listed in the [deployment view](../architecture/07-deployment-view.md#service-configuration): `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_API_URL` (defaults to the public GitHub REST API), and the existing `GITHUB_APP_KEY_FILE`. The key stays a file, never an environment value (`docs/architecture/02-constraints.md:12`).

## 7. Backfill

A historical backfill doesn’t record metric observations. Every milestone predates the store and fails the time gates, and the service has yet to export traces (`internal/telemetry/telemetry.go:4`). The glossary describes backfill as a source of traces without metrics (`docs/architecture/12-glossary.md:8`). Claim milestones that fail the gates. The webhook path and reconciler handle eligible milestones, including those for PRs that merge during backfill. Today a backfill would write `too_old` claims and a cursor to continue the walk. Each claim marks a milestone as seen without recording an observation (`docs/architecture/appendix-e-budget-dedupe-and-retention.md:40`). The same gates block those milestones on later runs, regardless of the claims.

`backfill.List` always builds page one (`internal/backfill/backfill.go:21-24`, `:59-75`) and needs a `Start` field to continue from another page. For a repository added to an existing store, a backfill may find milestones that pass both gates. Continue past those without claiming or recording them. Claim the ineligible milestones. The trace work adds a separate `traced` marker for live milestones. When traces or the open-PR table exist, the backfill can continue after a restart. Run one repository at a time through `backfill.List` at `budget.Backfill`. Apply `MaxRequests` per step and save `ResumeURL` in the `backfill_cursor` bucket after each step. Start the job when the store is new or when an `installation_repositories` body lists `repositories_added` (`docs/architecture/06-runtime-view.md:10`). Keep an absolute URL and use the client’s [existing host check, lines 249 to 260](../../internal/ghclient/ghclient.go), to reject another host. The job needs `node_id` and `updated_at` in `Record`. This design recommends deferring the job until then (open question 2).

## 8. Failure behavior

| Failure | Behavior |
| --- | --- |
| `ErrTokenSource`, or a `*StatusError` with 401 after the renewal | End the run with result `auth_failed`. Keep watermarks at or before the last committed values. Retry on the next tick |
| `*StatusError` 403 or 404 for one repository | Log it, skip the repository, finish the run with result `partial` |
| `ErrGaveUp`, or the run deadline | Stop the run with result `partial`. The budget has already backed off ([client backoff, lines 216 to 222](../../internal/ghclient/ghclient.go)) |
| `ErrBodyTooLarge`, or a decode error | Skip the repository, clear its first-page tag, result `partial` |
| A store error from a claim or a watermark write | Clear the first-page tag and return the error. Count the pass as `partial` and retry on the next tick. After three consecutive pass errors, return the last error to stop the service. The host supervisor restarts the service as described in the [deployment view](../architecture/07-deployment-view.md) |
| Collector down | Records go into cumulative memory and leave at the next accepted export. A crash during the outage loses them, as appendix A accepts (`docs/architecture/appendix-a-trace-and-metric-catalog.md:186-190`) |

## 9. Reconciler metrics and logs

The reconciler uses the meter from `telemetry.Setup`. It omits the repository attribute to limit the number of series to the low dozens.

| Instrument | Kind | Attributes | Meaning |
| --- | --- | --- | --- |
| `github_metrics.reconcile.runs` | Counter | `kind` (`incremental`, `sweep`), `result` (`ok`, `partial`, `auth_failed`) | One per finished run |
| `github_metrics.reconcile.duration` | Histogram, `s` | `kind` | Run wall time |
| `github_metrics.reconcile.repairs` | Counter | `kind`, `milestone` (`merged`, `ended`) | `RecordClosed` returned `recorded` on the repair path. The webhook path missed the milestone or hadn’t run yet |
| `github_metrics.reconcile.requests` | Counter | `kind`, `status` (`ok`, `not_modified`, `error`) | Responses the reconciler received |
| `github_metrics.reconcile.last_success` | Gauge, `s` | `kind` | Unix time of the last `ok` run. A staleness alert and the planned [readiness endpoint](../architecture/07-deployment-view.md#health-and-restarts) use this value |

The `repairs` counter can include more than missed deliveries. A reconciliation pass can record a milestone while its webhook delivery waits in the queue. The worker polls that queue every second.

For each run, the existing JSON handler (`cmd/github-metrics/main.go:67`) writes one summary with `kind`, `result`, `repos`, `pages`, `not_modified`, `recorded`, `already_counted`, `truncated`, and `duration`. On error it logs the summary at error level and omits the error text. Other run summaries log at info level. Each skipped or truncated repository gets a warning with its numeric repository ID and status. An `auth_failed` result gets an error line alongside the token source’s own line. All lines omit repository names, tokens, bodies, titles, and PR numbers.

## 10. Pull request split

Line counts are estimates of added Go lines, tests included, each under 400.

| PR | Content | Files | Estimate | Depends on |
| --- | --- | --- | --- | --- |
| A | `milestones` bucket, `ClaimMilestone`, `RepairFloor`, and watermark reads and writes | `internal/store/store.go`, `internal/store/store_test.go` | 250 | none |
| B | Add `ClosedPR` and `RecordClosed` with eligibility checks through the `Ledger` interface. Make webhook `Record` claim before any `Add` and add the `no_node_id` drop reason. Add `recorder.New(meter, ledger)` and pass the store from `main` to start claiming in this release | `internal/recorder/recorder.go`, `internal/recorder/recorder_test.go`, `cmd/github-metrics/main.go`, `cmd/github-metrics/main_test.go` | 360 | Merge after A. Development can proceed in parallel against a fake `Ledger` |
| C | Narrow the token request to metadata and pull requests read | [App token source](../../internal/appauth/appauth.go) and [its tests](../../internal/appauth/appauth_test.go) | 80 | none |
| D | Cache the repository list and add a bounded walk in order of update time. Clear tags on later pages. Return `ClosedPR` values and the newest `updated_at` | `internal/reconcile/list.go`, `internal/reconcile/list_test.go`, `internal/reconcile/BUILD.bazel` | 320 | none |
| E | One run: per-repository walk, `RecordClosed` calls, watermark advance, first-page tag clear, the failure table as a run result | `internal/reconcile/run.go`, `internal/reconcile/run_test.go`, `internal/reconcile/BUILD.bazel` | 320 | D and interfaces from A and B |
| F | Scheduler: startup run, 15-minute ticker, deadline, self-metrics and run logs | `internal/reconcile/loop.go`, `internal/reconcile/loop_test.go`, `internal/reconcile/BUILD.bazel` | 250 | E |
| G | Wire the source, budget, and client using the App config. Set the host environment and start the reconciler when its configuration is complete. Update pages 5 and 6. Document the `repair_floor` stamp, the `RECONCILE_FLOOR_RESET` flag, and the rollback procedure | `cmd/github-metrics/main.go`, `cmd/github-metrics/main_test.go`, `docs/architecture/05-building-block-view.md`, `docs/architecture/06-runtime-view.md`, `docs/architecture/07-deployment-view.md` | 220 | B deployed, C merged, F |
| H | Daily sweep at `budget.Sweep` | `internal/reconcile/loop.go`, `internal/reconcile/loop_test.go` | 120 | F |
| I | Backfill job and cursor, if open question 2 says now | `internal/backfill/backfill.go`, `internal/backfill/backfill_test.go`, `internal/store/store.go`, `internal/store/store_test.go`, `cmd/github-metrics/main.go` | 350 | A, G |

Work on A, C, and D can proceed in parallel and each can merge independently. Work on B can proceed alongside them, with B merging right after A. Start E once D merges because both change `internal/reconcile/BUILD.bazel`. Deploy B before G turns on reconciliation so the webhook path already claims milestones. Otherwise a repair can count a webhook delivery a second time. Merge C before G to narrow the token request. B’s wiring makes the first call to `RepairFloor`. A alone never stamps the cut-over. `bazelisk run //:gazelle` regenerates the `BUILD.bazel` files in D, E, and F (see the [monorepo instructions](../../../../AGENTS.md)).

## 11. Open questions for a human

1. Metric names: the code emits `github.pr.merges` and `github.pr.cycle_time` (`internal/recorder/recorder.go:52-53`, `:72`) with a `repository` attribute, while appendix A and D10 name `github.pull_request.merged` and `github.pull_request.cycle_time` with `vcs.repository.name` and `github.author.type` (`docs/architecture/appendix-a-trace-and-metric-catalog.md:105`, `:131`, `:139`). Does the repair keep today's names, or does a rename come first? A rename starts new series (`docs/architecture/decisions/0010-repository-and-author-type-are-the-only-metric-a.md:5`).
2. Backfill doesn’t record an observation until traces or the open-PR table exist. Build the cursor alone now, or defer PR I?
3. Appendix A says all telemetry content comes from REST (`docs/architecture/appendix-a-trace-and-metric-catalog.md:52`). Today the recorder reads closed webhook bodies. This design keeps that behavior behind the shared claim. Should the webhook path serve solely to trigger reconciliation through REST, as page 4 plans (`docs/architecture/04-solution-strategy.md:7`)?
4. The upgrade cut-over gives up repairing misses from the 24 hours before the upgrade. Acceptable?
5. Is `github_metrics.reconcile.` the right prefix? Should any of these instruments include the repository attribute? D10 covers PR metrics.
6. Should the sweep hour (03:00 Coordinated Universal Time in this proposal) and the 50-page and 10-page caps be constants or operator settings?
7. Token narrowing (PR C) and `iss` as App ID versus client ID: fix the code, or correct appendix B?
