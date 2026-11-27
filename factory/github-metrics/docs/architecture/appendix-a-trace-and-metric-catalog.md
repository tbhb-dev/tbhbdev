# Appendix A: trace and metric catalog

> Today versus design: this page describes the design, not running code. The code has a webhook receiver, a request budget, and a GitHub client. The worker, reconciler, store, and OpenTelemetry (OTel) export remain ahead. The original design is the source of each rule.

The original design specifies the planned trace of one pull request (PR) and the planned metrics. Every rule here is new, proposed by that design.

## ID derivation

Let `N` be the PR's GraphQL node ID and `H(x)` the Secure Hash Algorithm (SHA) digest SHA-256 of the UTF-8 bytes of `x`.

| Part | Planned rule |
| --- | --- |
| Trace ID | The first 16 bytes of `H("github-metrics/v1/trace/" + N)` |
| Span ID | The first 8 bytes of `H("github-metrics/v1/span/" + N + "/" + kind + "/" + key)`, with the kind and key from the span table that follows |
| Zero IDs | OTel rejects an all-zero ID. If a truncated digest is all zero, the service hashes the full digest again and truncates that |
| Version | The `v1` prefix lets a later scheme move every new span to fresh IDs without touching old traces |

Collision odds follow the birthday bound, about `n^2 / 2^(b+1)` for `n` IDs of `b` bits. A billion trace IDs give about 2^-69, and a thousand span IDs in one trace give about 2^-45. The design accepts both and doesn't check for collisions.

## Span tree

| Span name | Kind and key | Start | End | Emitted at |
| --- | --- | --- | --- | --- |
| `github.pull_request` | `root`, empty key | PR `created_at` | `merged_at` or `closed_at` of the first terminal state | First merge or close, once |
| `github.pull_request.commit` | `commit`, commit hash | Author date | Committer date | When the commit appears on the PR |
| `github.pull_request.review_request` | `review_request`, with the ID of the timeline event as key | The timeline event's `created_at` | The reviewer's next review, or the request's removal | At that end |
| `github.pull_request.review` | `review`, review node ID | Review `submitted_at` | Review `submitted_at` | When GitHub reports the review as submitted |
| `github.pull_request.merge` | `merge`, empty key | Last approving review, or PR creation without one | `merged_at` | At merge |
| `github.pull_request.close` | `close`, the ID of the `closed` event on the timeline | The latest of the last review, the last reopen, and PR creation | `closed_at` | At each close without merge |
| `github.pull_request.reopen` | `reopen`, the ID of the `reopened` event on the timeline | The event's `created_at` | The same time | When the reopen appears on the timeline |
| `github.pull_request.event` | `event`, the ID of the timeline event | The event's `created_at` | The same time | For a root event that happens after the service exported the root |

Every run derives the root span ID from `N` alone and uses it as the parent ID for each child. Review spans have zero duration. The review request span records the wait. A span for a commit measures the time from authoring to committing, which for most commits is zero or on the order of seconds.

Each span takes its start and end times from GitHub's own timestamps, never from the time the service saw the event.

## Reopen and the exported root

In the design, the service exports one root span per PR. That happens at the first merge, or at the first close. Nobody has checked how each backend treats a duplicate span (unverified). The service never sends the same span ID twice.

After a reopen, the PR keeps its trace and its one root. The reopen becomes a `github.pull_request.reopen` child. A later close gets its own `close` child keyed by its timeline event, and a later merge gets the `merge` child. Any root event after the root's export becomes a zero-duration `github.pull_request.event` child with the attribute `github.event.name`.

In a reopened PR, the end time of the root span and `vcs.change.state` describe the first terminal state rather than the latest one. Readers find the latest state on the last `merge` or `close` child.

## Root span events

Root events take GitHub's timestamps, and every one of them comes from the REST timeline. When the service builds the root at the first terminal state, it adds these events to the root.

- `ready_for_review` and `convert_to_draft`, from the timeline events of the same names.
- `head_ref_force_pushed`, from the timeline.

The design doesn't record review thread resolution. Neither the REST timeline nor the review comment endpoints expose a thread's resolved state, according to the GitHub Docs pages the original design cites. A webhook payload has no GitHub timestamp for the change (unverified). All telemetry content comes from REST responses. For that reason, the planned App doesn't subscribe to `pull_request_review_thread`.

Review comments don't get a span or an attribute, because the service doesn't read comment text and a submitted review already marks the moment.

## Span attributes

| Attribute | Spans | Example |
| --- | --- | --- |
| `vcs.provider.name` | All | `github` |
| `vcs.owner.name` | All | `tbhb-dev` |
| `vcs.repository.name` | All | `wiki` |
| `vcs.change.id` | All | `52` |
| `vcs.change.state` | Root | `merged`, `closed`, or `open` |
| `vcs.ref.base.name` | Root | `main` |
| `github.pull_request.author` | Root | The author's login |
| `github.author.type` | Root | `user` or `bot` |
| `github.review.state` | Review | `approved`, `changes_requested`, or `commented` |
| `github.review.reviewer` | Review and review request | A login or team slug |
| `vcs.ref.head.revision` | Commit | The commit hash |
| `github.event.name` | Event | `ready_for_review` |

The `vcs.*` names follow the OTel semantic conventions for version control, which are still in development, so their exact names are unverified. Names under `github.*` are this service's own. Titles, bodies, comment text, and diffs never appear. Repository names, branch names, logins, and team slugs from private repositories do appear, and the original design's section “Data the service keeps and sends” covers who sees them.

## Metric catalog

All instruments come from the OTel SDK inside the service and record metrics directly. See [Direct metrics, not derived from traces](#direct-metrics-not-derived-from-traces).

Duration histograms, all in seconds:

| Instrument | Start | End |
| --- | --- | --- |
| `github.pull_request.time_to_first_review` | PR `created_at` | `submitted_at` of the first review whose author isn't the PR author |
| `github.pull_request.time_to_approval` | PR `created_at` | `submitted_at` of the first `APPROVED` review |
| `github.pull_request.time_to_merge` | PR `created_at` | `merged_at` |
| `github.pull_request.time_from_first_commit_to_merge` | Commit time of the PR's first commit | `merged_at` |
| `github.pull_request.time_from_first_commit_to_open` | Commit time of the PR's first commit | PR `created_at` |
| `github.pull_request.time_from_last_commit_to_merge` | Commit time of the PR's last commit | `merged_at` |
| `github.pull_request.time_from_approval_to_merge` | `submitted_at` of the last `APPROVED` review before the merge | `merged_at` |

For these histograms, the commit time is `commit.committer.date`, the one time used for the first-commit and last-commit histograms and for the cycle time start. A PR opened before its first push can produce a negative duration, and the worker skips that observation.

Size histograms, recorded at merge:

| Instrument | Unit | Value |
| --- | --- | --- |
| `github.pull_request.commits` | `{commit}` | The PR's `commits` count |
| `github.pull_request.changed_lines` | `{line}` | The PR's `additions` plus `deletions` |
| `github.pull_request.reviewed_heads` | `{head}` | The number of distinct commits that reviews of the PR name as the reviewed commit |

Counters:

| Instrument | Unit | Counts | Extra attribute |
| --- | --- | --- | --- |
| `github.pull_request.merged` | `{pull_request}` | Each merged PR once | None |
| `github.pull_request.reviews` | `{review}` | Each submitted review once | `github.review.state`: `approved`, `changes_requested`, `commented`, or `dismissed` |

A review counts under the state it has when the worker first sees it. A later dismissal doesn't move it. Pending reviews don't count.

Gauges, read from the store at each collection:

| Instrument | Unit | Value |
| --- | --- | --- |
| `github.pull_request.open` | `{pull_request}` | The number of open PRs |
| `github.pull_request.oldest_open_age` | `s` | The age of the oldest open PR, from its `created_at` |

An open-PR table joins the store, with the node ID, repository, author type, and `created_at` of each open PR. When a PR opens or reopens, the worker adds a row. It removes the row at merge or close, and the reconciler repairs rows that a missed webhook left wrong. A repository with no open PR reports 0 for the count and omits the age. When the service is down, both series go stale and the dashboard goes empty. Gauge values are state, so they can't double count, and a lost store refills from the backfill.

The receiver keeps the main status gauge in memory and updates it from `check_run` webhooks. A restart clears its value, and the backfill doesn’t refill it:

| Instrument | Unit | Value |
| --- | --- | --- |
| `github.main.red` | None | `1` when the latest `conclusion` check on the default branch failed or timed out, `0` otherwise, keyed by `repository` |

After a receiver restart, the first main `conclusion` check adds the repository to the gauge.

To measure cycle time, the histogram counts the seconds from the start of the first agent run that contributed to the PR until the PR ends:

| Instrument | Start | End | Extra attributes |
| --- | --- | --- | --- |
| `github.pull_request.cycle_time` | The earliest of every `started_at` on an identity trailer line in the PR's commits or body, and the commit time of the PR's earliest commit | `merged_at`, or `closed_at` for a PR closed without a merge | `vcs.change.state`: `merged` or `closed`. `github.pull_request.start_source`: `started_at` or `commit`, whichever supplied the start |

A non-agent commit sets the start if it predates the first agent launch. Lines with `started_at` come from both Generated-by and Assisted-by trailers, in the commits, and in the PR body. No trailer has `started_at` today, so PRs record the earliest commit time and the source `commit`. Once a PR has an earlier `started_at`, the source switches. The trailer defines `started_at` in whole seconds of Coordinated Universal Time (UTC), in the form `YYYY-MM-DDTHH:MM:SSZ`. When reading a trailer, the worker keeps that field and drops the rest of the text. A closed PR is a separate end state from a merged one. Work before the first agent run or commit doesn't count. Always consider the earliest commit time as a candidate for the start.

Count the first time a PR ends. If a reopened PR ends again, the stored `ended` milestone blocks a second observation.

For each completed CI job, the service records the time it spent in the queue and the time it spent running, regardless of the conclusion:

| Instrument | Unit | Start | End | Attributes |
| --- | --- | --- | --- | --- |
| `github.ci.job.queue_seconds` | `s` | Job `created_at` | Job `started_at` | `repository`, `workflow`, `ci_job`, `conclusion`, `event_class` and `runner` |
| `github.ci.job.run_seconds` | `s` | Job `started_at` | Job `completed_at` | `repository`, `workflow`, `ci_job`, `conclusion`, `event_class` and `runner` |

The workflow and job names come from `workflow_name` and `name`. The job name uses the attribute `ci_job` because the scrape `job` label overwrites a `job` attribute. The `ci_job_failures_total` counter uses `ci_job` too, and the log fields keep the name `job`. Take the runner from the first `labels` entry, or use `none` for an empty or missing array. Each attribute removes line breaks and keeps at most 64 characters. Both histograms use explicit boundaries of 5, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 2700, 3600, 5400, and 7200 seconds. A missing or invalid timestamp skips that duration. A negative duration also skips that observation. The ledger claims the decimal job ID followed by a zero byte and `ci_job` before recording. Jobs without an ID skip recording. If the claim already exists, skip all metrics and logs for the delivery. Retry the delivery if the claim fails.

The `event_class` attribute approximates the trigger because the payload has no triggering event. A `head_branch` with the prefix `gh-readonly-queue/` counts as `merge_group`. Classify the repository’s default branch as `push`. Any other branch counts as `pull_request`. Scheduled and manual runs on the default branch count as `push`, and runs on a non-default branch from other triggers count as `pull_request`.

Every PR histogram is an exponential (native) histogram, with the SDK's default limit of 160 buckets. Over a range of 1 second to 30 days, 160 buckets give a scale of 2, which means each bucket spans a ratio of about 1.19 and a quantile estimate is off by about 9% at most. Goal thresholds of 600 s and 900 s sit inside a bucket, not on an edge, so a goal check has the same error. D15 records the choice, the fallback, and the backend check. Each attribute set exports one native histogram series, with the count and sum inside it, if Mimir ingests native histograms (unverified).

PR metrics use `vcs.repository.name` and `github.author.type`, with `user` or `bot` for the author type. The review counter adds `github.review.state`, and the cycle time histogram adds `vcs.change.state` and `github.pull_request.start_source`.

### Direct metrics, not derived from traces

The service records durations itself because Tempo's metrics generator can't derive them from this service's spans. It drops spans that ended more than 30 seconds before ingestion (`MetricsIngestionSlack`, default 30 s, in `modules/generator/config.go` of Tempo 3.1.0), and the service backdates its spans to GitHub's timestamps. The deployment runs Tempo 3.1.0 and keeps the default slack of 30 s. Span metrics from Alloy are an optional extra and never the source of these series. D14 records the decision.

### Cardinality budget

| Attribute | Values | Assumption |
| --- | --- | --- |
| `vcs.repository.name` | About 50 | Tens of repositories today, with headroom |
| `github.author.type` | 2 | `user` or `bot` |

Each instrument has 50 × 2 = 100 attribute sets, except the main status gauge, which has 50 sets and uses one attribute, `repository`. The review states give the review counter 400, and the end states and start sources give the cycle time histogram 400. Series counts below are for native histograms and for the explicit-bucket fallback of D15, which has 15 boundaries, 16 buckets, a sum, and a count, so 18 series per attribute set.

| Instruments | Native histograms | Explicit fallback |
| --- | --- | --- |
| 10 histograms, 100 attribute sets each | 1,000 | 18,000 |
| Cycle time histogram, 400 attribute sets | 400 | 7,200 |
| Merged counter, 100 attribute sets | 100 | 100 |
| Review counter, 400 attribute sets | 400 | 400 |
| 2 gauges, 100 attribute sets each | 200 | 200 |
| Main status gauge, 50 attribute sets | 50 | 50 |
| Total | 2,150 | 25,950 |

A cardinality limit in the SDK caps every instrument at 2,000 attribute sets. Sets beyond the cap fold into its overflow series.

The author's login stays out. Public repositories accept PRs from anyone, which makes the set unbounded, and per-person timing invites a use of the data nobody asked for. Nor does the PR number ever become a metric attribute.

### Recording rule

1. After each reconcile, the worker checks the PR's milestones. The first three durations are milestones of their own. The merge is a fourth milestone, `merged`, that triggers every other merge-time instrument. The first merge or close is a fifth, `ended`, that triggers the cycle time histogram. Each submitted review is a milestone keyed by the review's node ID.
2. Eligibility needs GitHub's state to show the milestone and the store to lack a record of it, provided the milestone's end timestamp is both less than 24 hours old and later than the store's creation time. Each milestone counts once, keyed by the PR's node ID and the milestone name, or by the review's node ID for a review.
3. Next, the worker commits `(N, milestone)` to the store in its own synced transaction.
4. The worker waits for a successful commit before recording the observation. For `merged`, that means the merged counter and the commit-to-merge, approval-to-merge, commits, changed-lines, and reviewed-heads histograms in one pass. If the commit fails, the worker doesn't record anything, and the next reconcile tries again.
5. Any milestone that isn't eligible on age or store creation time gets a store record and no observation, so backfills and long outages never produce a spike of old values.

The worker commits first and records second. A crash between steps 3 and 4 permanently loses that one observation, because the store already marks the milestone as counted. The design accepts a missed count over a double count. Backfilled history gets traces and no metrics. Gauges don't follow this rule, because they report the store's current state.

### Dashboard and recording rules

Teams that report PR timing today read it from GitHub by script. The service's own series replace that, in two parts.

Recording rules in Mimir compute, per repository over 24 hours, the median and p95 of `time_to_merge`, which is PR created to merged. They also compute the median and p95 of the first-commit and cycle time histograms, and the count of merged PRs. A goal flag per repository, `met` or `missed`, covers the repositories with a turnaround goal. The goal is a median of 10 minutes and a p95 of 15 minutes from PR created to merged. Cycle time is a separate figure and doesn't replace the goal. With native histograms the flag reads the quantile directly, with the error that D15 states.

A Grafana dashboard then shows the rule outputs, the open PR count and oldest open age, the review counter by state, the cycle time by end state and start source, and the size histograms. It replaces the scripted timing table. Both live in the observability configuration of the platform repository, and step 8 of the plan covers them.

### Metric loss on a crash

Metrics leave through the SDK's periodic reader every 60 seconds with cumulative aggregation, which keeps running totals in memory instead of a queue of observations. A crash or restart loses every observation recorded since the last export the collector accepted. With successful exports on schedule, that’s at most 60 seconds. During a collector outage, though, the loss covers the whole outage, with no time bound.

Memory use doesn't grow during an outage, because cumulative state keeps one total per attribute set, capped at 25,950 series. Lost observations stay lost, since the worker already committed their milestones and won't record them a second time. Backends see a counter reset.

### Store loss and restore

The store is the record of what the service already exported, so losing it or rolling it back makes the service export again.

- **Lost or corrupt store.** The service starts with a new empty store and records the store's creation time. The backfill then exports every span of every PR in scope a second time with the same IDs, and the backend's handling of those duplicates is unverified. Metrics don't double, because step 2 skips any milestone that ended before the store's creation time.
- **Restored older snapshot.** The operator must not restore an older copy of the store, because the snapshot keeps its old creation time. If someone restores one anyway, every span exported after the snapshot exports again, and every milestone counted after the snapshot that's still under 24 hours old counts twice.
- **Instead of a restore,** the operator deletes the damaged file and lets the service start empty. The threat-model follow-up, [plan step 6 on page 11](11-risks-and-technical-debt.md), sets the store's file modes and corruption handling, and it decides whether anyone backs up the store at all.
