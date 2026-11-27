# Review metrics design

Status: proposal, not reviewed, with no code written. The `file:line` references point at `origin/main` at `3424780`. This design follows the code wherever the architecture pages disagree with it. The [reconciler design](reconciler-backfill.md) made the same choice.

## 1. Goal and scope

Agents write and review every change here. Today a one-off script computes review numbers from one GraphQL read and discards them when it exits. This design moves those answers into the service as metrics that dashboards and alerts can query.

The questions:

- Reviews per pull request (PR), and the median and top count of reviews per merged PR.
- Reviews that approve or request changes, and reviews that GitHub dismisses.
- Findings per review for each priority and for each kind.
- The share of first reviews that approve.
- Approvals that still list findings.
- Approvals that a later push dismisses.
- The wait between a changes-requested review and the next review on the same PR.
- Findings per merged PR across all rounds.

Section 4 adds two timings. It also explains each metric left out.

Out of scope are spans for reviews, which appendix A plans alongside the other traces, and any metric that needs a diff or comment text or the resolved state of a review thread.

## 2. Existing pieces

| Piece | Where | Use in this design |
| --- | --- | --- |
| Webhook path | `internal/worker/worker.go:91-98`, `internal/recorder/recorder.go:180-240` | The worker hands each queued body to `Record`. Today a `pull_request_review` delivery adds 1 to `github.events` and records nothing else (`recorder.go:336-344`). `tracksPullRequest` matches two actions of `pull_request`: `closed` and `reopened` (`recorder.go:243-245`) |
| Milestone claims | `internal/store/store.go:246-264` | `ClaimMilestone` writes a key once in a synced transaction. The CI job path already claims by a key that isn't a PR (`recorder.go:424`) |
| Eligibility gates | `recorder.go:586-612` | A milestone counts when under 24 hours old and later than the store's creation time. The recorder skips it otherwise. Repairs also respect the repair floor |
| Body reading | `recorder.go:639-660` | `cycleStart` reads a PR body for trailer fields and keeps nothing else, a precedent for parsing a review body in memory |
| Reconciliation walk | `internal/reconcile/list.go:251-326`, `run.go:238-296` | A walk in order of update time per repository. `absorb` drops items without a close time (`list.go:318`), and `FirstEnd` reads issue events per PR (`events.go:45-80`) |
| Review template | `factory/cli/src/tbhbdev/alpha/review_data/PULL_REQUEST_REVIEW_TEMPLATE.md` | Guidance for reviewers |
| Review grammar | `factory/cli/src/tbhbdev/schemas/review.cue:14`, `:107`, `:135`, `:145`, `:150-156` | The schema that `tbhbdev` checks every posted review against, and the source of truth for the finding line |
| Soft dismissal | `factory/cli/src/tbhbdev/alpha/review_doc.py:274-310` | `tbhbdev alpha review dismiss` edits a body to start with `SUPERSEDED:` and never calls GitHub's dismissal |
| Main branch rules | `lib/rules/repo/baseline/files/.github/rulesets/main.json:28-36` | A push dismisses stale approvals, the last push needs approval, and each review thread must resolve before merge |

In code the metric names are `github.pr.merges` and `github.pr.cycle_time`, with a `repository` attribute and explicit buckets (`recorder.go:147-152`, `:291`, `:309`). Appendix A and D15 call for `github.pull_request.*`, `vcs.repository.name`, and exponential histograms. Open question 1 of the reconciler design leaves that choice open. This design follows the code and renames along with the rest if the answer changes.

## 3. Review body parser

### Body format

The schema allows four sections in a fixed order: `## Summary`, an optional `## Findings`, an optional `## Suggestions`, and `## Verification`. Findings allows up to eight bullets. Each bullet's text must match `#FindingPrefix` at `review.cue:14`:

```text
^F([1-8]): P([0-3]) (correctness|security|design|test|docs)\s+([\s\S]+)$
```

That differs from the task brief in three ways. Priorities run from P0 to P3, not P1 to P3. A finding cites a `path:line` or a check and its result, and the grammar doesn't require a `path:line` at all. Posted bodies end with the `Generated-by: LLM key=value;…` trailer or with an HTML marker, `<!-- tbhb-review v2 <hash> -->`, in either order (`review.cue:135-137`). Among the trailer's attributes are `harness`, `model`, and `effort`. Priority also depends on the review event: P0 and P1 require `REQUEST_CHANGES`, and `REQUEST_CHANGES` requires one of them (`review.cue:142-147`). An approval can list P2 and P3 findings and nothing higher.

### Parser package

The new `internal/review` package turns a body into enums and counts. Its one function, `Parse(body string) Result`, returns a `Parsed` flag and a fixed reason when parsing fails. It also returns the count of findings for each pair of priority and kind, plus the trailer's `harness`, `model`, and `effort` values. Body text, finding text, and `path:line` values never leave the function.

Steps:

1. Drop a first line that starts with `SUPERSEDED:` and note that it existed.
2. Split the body on lines that start with `##` and a space. Require `Summary` and `Verification`, and accept `Findings` and `Suggestions`.
3. Strip the leading dash and space from each bullet in `Findings`, then match it against the same expression as `#FindingPrefix`. Numbers must run from F1 without gaps.
4. Read the last line that starts with `Generated-by: LLM` and split its attribute list on semicolons and equals signs.

Strict parsing beats lenient parsing here. One bad bullet in `Findings`, a gap in numbering, or a missing required section sets `Parsed` to false, and the review then counts zero findings. A partial count would hide template drift behind plausible numbers. The fixed reasons are `no_sections`, `bad_finding`, and `no_trailer`. A missing trailer blanks the trailer fields and leaves the findings counted. A human review in free text fails as `no_sections`.

### Matching the template

The schema in `review.cue` defines the grammar. The parser and the schema share one set of example bodies, and tests on both sides read the same files. Neither side can change alone.

- A new folder of review bodies under `factory/cli/src/tbhbdev/schemas/testdata/review-bodies/` keeps valid and invalid examples, each with a small expected-result file beside it. The existing `schemas` file group has visibility limited to `factory/cli` (`factory/cli/BUILD.bazel:101-105`). A new file group for the folder opens it to `//factory/github-metrics/internal/review`.
- A Python test in `factory/cli` runs `content.check_review` on each body and checks that valid examples pass and invalid ones fail.
- A Go test parses each body and compares the result with the expected file.

Changing `#FindingPrefix` or the section list breaks the Python test until someone updates the examples. Updated examples then break the Go test until someone updates the parser. In production, a rising `findings="unparsed"` rate on `github.pr.reviews` signals drift that the examples missed.

### Telemetry content

D12 forbids titles, bodies, comment text, and diffs in telemetry. Metrics contain the parsed enums and counts and nothing from the text. Section 6 adds one log line per review with those enums, the numeric review ID, the repository, and the trailer's `harness`, `model`, and `effort`. Those trailer values identify a tool and contain nobody's words. Webhook bodies with review text already sit in the queue under appendix E's retention. The service discards REST bodies after parsing and doesn't store a body anywhere.

## 4. Metrics

### Instruments

| Instrument | Kind and unit | Attributes | Recorded when | Answers |
| --- | --- | --- | --- | --- |
| `github.pr.reviews` | Counter, `{review}` | `repository`, `state`, `findings` | Each submitted review, once | Reviews posted by state, approvals with findings, and the denominator for findings per review |
| `github.pr.review.findings` | Counter, `{finding}` | `repository`, `priority`, `kind` | With the review that lists them | Findings by priority and by kind |
| `github.pr.review.first_reviews` | Counter, `{review}` | `repository`, `state` | The first review on a PR, once per PR | Share of first reviews that approve, and the denominator for reviews per PR |
| `github.pr.review.dismissals` | Counter, `{review}` | `repository`, `cause` | Each dismissal or supersession, once per review | Approvals dismissed by a later push, and all dismissals |
| `github.pr.review.wait_after_changes` | Histogram, `s` | `repository` | A review that follows a changes-requested review on the same PR | Time from changes requested to the next review |
| `github.pr.review.first_wait` | Histogram, `s` | `repository` | The first review on a PR | Time from ready for review to first review |
| `github.pr.review.reviews_per_merge` | Histogram, `{review}` | `repository` | Each merged PR, once | Median and top reviews per merged PR |
| `github.pr.review.findings_per_merge` | Histogram, `{finding}` | `repository` | Each merged PR, once | Findings per merged PR across all rounds |
| `github.pr.review.inline_comments` | Counter, `{comment}` | `repository` | Each top-level review comment on a diff line, once | Inline findings against body findings, after inline posting merges |

Attribute values:

- `state`: `approved`, `changes_requested`, `commented`, or `dismissed`. Following appendix A, a review counts under its state at the moment the service first sees it. The value `dismissed` comes from a repair that finds a review already dismissed, and from nowhere else.
- `findings`: `none` when the body parses without a Findings section, `some` when it parses with at least one finding, and `unparsed` otherwise.
- `priority`: `p0`, `p1`, `p2`, or `p3`. `kind`: `correctness`, `security`, `design`, `test`, or `docs`.
- `cause`: `stale_head` when the commit of the dismissed review differs from the PR head at dismissal, `current_head` for any other GitHub dismissal, and `superseded` for an edit that adds `SUPERSEDED:`.

### Counted reviews

A review counts once GitHub reports it submitted, provided someone other than the PR author wrote it and its body isn't empty. A reply to an inline comment may arrive as a `commented` review with an empty body (unverified), and the empty-body rule drops those either way. GitHub doesn't deliver pending reviews to the service.

### Reasons for each metric

- **Reviews by state, and dismissals by cause.** These give the base rates. After a model or prompt change, an operator checks the approval rate first.
- **Findings by priority and kind.** A rise in `p1` `correctness` findings points at the writing agents. A rise in `p3` `docs` findings may point at a noisy reviewer instead.
- **First reviews by state.** If most first reviews approve, the review round adds little. If almost none do, the writing agents miss what reviewers catch.
- **Approvals with findings** (`state="approved", findings="some"`). The schema lets an approval list P2 and P3 findings. Agents seldom return to them after merge, so this count measures debt that agents merge knowingly.
- **Dismissals with `cause="stale_head"`.** Under the main branch rules a push dismisses stale approvals, and each dismissal costs another full review. GitHub's push dismissal applies to approvals, which makes this count the number of approvals lost to a later push. It prices pushes after approval.
- **Wait after changes requested.** Here the PR waits first for a fix and then for a reviewer. When long, this wait dominates turnaround, and the existing timing histograms can't isolate it.
- **Wait for the first review.** Appendix A sets a turnaround goal of a median of 10 minutes from open to merge. Reviewers control the stretch from ready for review to the first review. Measuring from `created_at` would count draft time that nobody waits on.
- **Reviews and findings per merged PR.** Together these measure the review rounds and the issues raised for each merged change.
- **Inline comments.** A change in progress posts each finding that cites a diff line as an inline comment too. Comparing inline comments with findings checks that change. Before it merges the count means nothing, so it comes last.

### Excluded metrics

- **Review rounds to approval.** For a merged PR the last review before merge approves, so the reviews-per-merge histogram already answers this. A second instrument would duplicate it.
- **Resolved against unresolved threads at merge.** The main branch rules require every thread resolved before merge (`main.json:36`), which makes the value zero on `main`. REST also can't read a thread's resolved state (D11, appendix A).
- **Findings fixed later against findings dropped.** Matching a finding to a later commit needs the diff and the finding text. D11 excludes the first, and D12 keeps the second out of telemetry.
- **Review counts per reviewer model.** Section 10 and open question 1 cover this one.

## 5. Events, fields, and App changes

| Source | Actions or call | Fields read | Feeds |
| --- | --- | --- | --- |
| `pull_request_review` webhook | `submitted` | `review.node_id`, `review.id`, `review.state`, `review.body`, `review.submitted_at`, `review.commit_id`, `review.user.login`, `pull_request.node_id`, `pull_request.user.login`, `repository.full_name` | Reviews, findings, first reviews, both waits |
| `pull_request_review` webhook | `dismissed` | `review.node_id`, `review.commit_id`, `pull_request.head.sha`, `pull_request.node_id`, `pull_request.updated_at` | Dismissals with `stale_head` or `current_head` |
| `pull_request_review` webhook | `edited` | `review.node_id`, `review.body`, `changes.body.from`, `pull_request.updated_at` | Dismissals with `superseded`, when the new body starts with `SUPERSEDED:` and the old one didn't |
| `pull_request` webhook | `opened`, `ready_for_review`, `converted_to_draft`, `reopened` | `pull_request.node_id`, `pull_request.draft`, `pull_request.created_at`, `pull_request.updated_at` | The ready time in section 7 |
| `GET /repos/{o}/{r}/pulls/{n}/reviews` | Repair walk | `node_id`, `id`, `state`, `body`, `submitted_at`, `commit_id`, `user.login` | Repairs of everything listed in this table, and both per-merge histograms |
| `GET /repos/{o}/{r}/issues/{n}/events` | Repair walk | `event` of `ready_for_review`, `convert_to_draft`, and `review_dismissed`, with `created_at` | The ready time and dismissal times on repair |
| `GET /repos/{o}/{r}/pulls/{n}/comments` | Repair walk, without a webhook | `node_id`, `pull_request_review_id`, `in_reply_to_id`, `created_at` | Inline comments |

GitHub's documented schemas list these payload fields. Nobody here has captured a live delivery. Until a test runs against a recorded body, treat each field as unverified. On a `ready_for_review` delivery, `pull_request.updated_at` approximates the event time and may differ from the timeline's `created_at` by seconds (unverified). The repair path reads the timeline instead.

### Permissions and subscriptions

Pull requests read covers the reviews list, the review comments list, and the issue events list (appendix B). After the reconciler design narrows the token to metadata and pull requests, that still suffices. Permissions match today's.

Appendix B lists `pull_request` and `pull_request_review` as the two subscribed events. In code, the service also handles `check_run` and `workflow_job` (`recorder.go:224-239`), so appendix B lags the live App. Nobody here can read the live App's subscriptions. Before the webhook slice merges, the operator confirms that `pull_request_review` deliveries arrive by checking for a `github_events_total{event="pull_request_review"}` series. Every delivery already adds to that series.

This design doesn't subscribe to `pull_request_review_comment` or `pull_request_review_thread`. Their payloads would put comment text and diff hunks in the queue, and D11 excludes both. Inline counts come from the repair walk alone, at one request per changed PR and without a new subscription. D11 stays as written. Appendix B changes in one place: the `pull_request_review` row adds `edited` to the actions used.

## 6. Recording

### Shared recording function

`recorder` gains `RecordReview(ctx, Review) (ReviewOutcome, error)`, shaped like `RecordClosed`. A `Review` contains the repository, the PR node ID, both review IDs, the state, the submit time, and the parsed `review.Result`. It also contains whether this is the PR's first counted review, the time of any previous changes-requested review, and the PR's ready time. A `Repair` flag records which path built it.

The webhook path builds a `Review` from the body and fills in context from the cached PR record in section 7. Each item of the reviews list gives the repair path a `Review`, and the full ordered list gives it the context. Because both paths call one function, both produce the same series.

### Claims

As D10 requires, each fact gets its own claim key, written before the observation:

| Key | Covers |
| --- | --- |
| review node ID, zero byte, `review` | `github.pr.reviews` and `github.pr.review.findings` for that review |
| PR node ID, zero byte, `first_review` | `github.pr.review.first_reviews` and `github.pr.review.first_wait` |
| review node ID, zero byte, `wait_after_changes` | `github.pr.review.wait_after_changes` for that review |
| review node ID, zero byte, `dismissed` | `github.pr.review.dismissals` with a GitHub cause |
| review node ID, zero byte, `superseded` | `github.pr.review.dismissals` with `cause="superseded"` |
| PR node ID, zero byte, `review_totals` | Both per-merge histograms |
| comment node ID, zero byte, `inline` | `github.pr.review.inline_comments` |

Each gate tests the fact's own time: `submitted_at` for a review and `merged_at` for totals. Neither the `dismissed` nor the `edited` payload has a time for the action itself. For those, the webhook path uses `pull_request.updated_at` from the same payload (unverified as a close match). On repair, a GitHub dismissal takes the `created_at` of its `review_dismissed` event. The reviews list lacks an edit time. A repair claims `superseded` without an observation, leaving supersessions to webhooks. As in `RecordClosed`, when a fact fails a gate, the recorder still writes its claim and skips the observation.

### Webhook path without context

A webhook may arrive for a PR missing from the cache. Then the webhook path lacks the first-review time and the time of any earlier changes-requested review. It records the review and its findings and leaves `first_review` and `wait_after_changes` unclaimed for the repair walk, which reads the full list and claims them later. Repair fills the gap. With a cached record, the webhook path claims both and records or skips each as the record dictates. Late webhooks return `already_counted` when a repair claimed the fact first.

### Per-merge totals

Counting reviews and findings at merge needs every review of the PR. A cache built from webhooks can miss one, and a recorded histogram observation can't change afterward. For that reason the repair walk alone records the two per-merge histograms. For each newly merged PR in its walk, it reads the full reviews list, at one request per merged PR inside the per-PR read the walk already makes. A deployment without reconciliation configured doesn't record per-merge totals and logs that fact once at startup.

### Log line per review

For each claimed review, the recorder logs one info line. It contains the repository, the numeric review ID, `state`, `findings`, the parse reason on failure, and the count per priority, along with the trailer's `harness`, `model`, and `effort`. Logs go to Loki, where LogQL can compare reviewer models without metric labels. Section 10 explains that choice.

## 7. State between events

A new bucket, `review_prs`, caches facts per PR that the webhook path needs and that REST can always rebuild. The key is the PR node ID. A value contains four times, each zero when unknown:

| Field | Set by |
| --- | --- |
| `ready_at` | An `opened` delivery with `draft=false` sets it to `created_at`, `ready_for_review` sets it to `updated_at`, and `converted_to_draft` clears it. The repair walk sets it from the latest `ready_for_review` event before the first review, or from `created_at` without one |
| `first_review_at` | The first counted review |
| `last_changes_requested_at` | Each `changes_requested` review, never moving backward |
| `updated_at` | Every write, for retention |

Apart from that clear, a write keeps the later of the stored and new times, which stops out-of-order deliveries from moving a field backward.

Size: a node ID takes about 20 bytes, and four Unix times in seconds take 32. Including the database's own overhead, a record takes under 100 bytes. Hundreds of open PRs across the installation put the bucket at tens of kilobytes.

Retention: an open PR keeps its record. Once the PR's `ended` milestone has a claim, the worker deletes the record. A daily pass, run alongside the reconciliation sweep, deletes records with an `updated_at` older than 30 days. A reopened PR without a record behaves like any PR missing from the cache, and the repair walk fills it in. Appendix E's data table gains a row for this bucket.

Each new claim adds about 40 bytes to the `milestones` bucket. At hundreds of reviews a day, the total stays under 5 MB a year. The existing claims already grow without limit, since `ClaimMilestone` never deletes a key (`store.go:246-264`). A future pruning pass for them can include these, since a claim older than 24 hours can't change any outcome.

## 8. Repair and backfill

### Repair walk

The walk already lists each repository's PRs by update time (`list.go:251-306`). This design changes it in two places:

1. `absorb` returns open items too, alongside items with a close time (`list.go:318`). The list request already asks for `state=all` (`list.go:186`) and doesn't change. The closed-PR path keeps its filter.
2. For each item, the walk reads the reviews list and calls `RecordReview` with `Repair` set for each review. For a newly merged item it also records the per-merge totals. When a PR's first review lacks a ready time, it reads issue events, which `FirstEnd` already fetches for closed items.

Later, the inline-comments slice adds one read of the review comments list for each item with a review in the window.

A `304` response returns no body (`events.go:43-44`). Like the issue events read, each per-PR read clears its entity tag first, which makes every read a full request.

Cost per walk, for about 30 PRs changed an hour:

| Read | Requests an hour |
| --- | --- |
| Reviews list, one page per changed PR | 30 |
| Issue events for open PRs missing a ready time | Up to 30, falling close to zero once the cache fills |
| Review comments list, after inline posting merges | 30 |
| Total added | Up to 90 |

Appendix E budgets 120 requests an hour for per-PR reads on changed PRs. This fits within that line, at a steady state of about 9% of the 5,000-request floor. A PR with more than 100 reviews needs a second page.

The walk finds a PR when its `updated_at` moves, and misses it otherwise. Nobody has checked whether a submitted review or a dismissal moves `updated_at`, and the reconciler design flags the same assumption for closes (`reconciler-backfill.md:79`). If a review leaves it unchanged, a missed review webhook on an idle PR goes without repair until the PR changes again. A merge always changes it, so per-merge totals remain exact either way. Before the repair slice merges, someone checks this against a live PR.

### Backfill

Backfill costs zero requests, because no historical walk runs for reviews. When a repair meets a review older than the gates, it writes the claim without an observation, as for every other metric. The gates reject anything older than 24 hours or earlier than the store's creation time (appendix A, step 5), and a cumulative counter can't take a backdated point. After deploy, the first incremental run covers PRs updated in the last 24 hours. Since the repair path rebuilds context from the full reviews list, the cache doesn't need seeding.

The script's numbers for past weeks don't reappear as series. Open question 2 covers that loss.

## 9. Dedupe, redelivery, and edge cases

| Case | Behavior |
| --- | --- |
| The same delivery twice | The receiver drops a repeated body hash (D6). A delivery with a new hash for an old review hits the claim and returns `already_counted` |
| Webhook and repair both see a review | The first claim records, and the other path returns `already_counted` |
| A review edited after posting | Its counts remain as first parsed, matching appendix A's rule for state. An edit that adds `SUPERSEDED:` counts as a dismissal, and other edits don't change any count |
| A review dismissed by GitHub | One dismissal with `stale_head` or `current_head`. Its entry in `github.pr.reviews` keeps its original state |
| A review dismissed before the service saw it | The repair walk counts it under `state="dismissed"` with `findings` from its body. It counts the dismissal at the time of the `review_dismissed` event. If that event lacks a usable time, the dismissal gets a claim and the walk moves on |
| A manual dismissal with the head unchanged | `review.commit_id` equals the head, so it counts as `current_head`. Dismissals from a push count as `stale_head`. A manual dismissal after an unrelated push also counts as `stale_head`, and the design accepts that error |
| A review on a head that's no longer current | It counts like any other review. `tbhbdev` refuses to post against a stale head (`review_doc.py:208`), so this happens in a race and nowhere else. The review still happened and still used a run |
| A reply to an inline comment | An empty `commented` review, which the empty-body rule drops |
| A reopened PR | Its reviews keep counting. `first_review` keeps its claim, so a reopen can't create a second first review. A second merge doesn't record totals again |
| A review by the PR author | Not counted, matching `time_to_first_review` in appendix A |
| A finding repeated in a later round | Counts again. The per-merge findings total measures findings raised, and repeats inflate it |

## 10. Cardinality

Appendix A assumes about 50 repositories. Against the SDK cap of 2,000, these are the worst-case attribute sets per instrument:

| Instrument | Product | Sets |
| --- | --- | --- |
| `github.pr.reviews` | 50 × 4 states × 3 finding classes | 600 |
| `github.pr.review.findings` | 50 × 4 priorities × 5 kinds | 1,000 |
| `github.pr.review.first_reviews` | 50 × 4 states | 200 |
| `github.pr.review.dismissals` | 50 × 3 causes | 150 |
| Each of the four histograms | 50 | 50 |
| `github.pr.review.inline_comments` | 50 | 50 |

Like the code today, the histograms use explicit buckets. For the waits, the edges are 30, 60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 14400, 28800, 86400, 172800, and 604800 seconds, with 600 and 900 on edges as in D15. The per-merge counts use every integer from 0 to 10, then 12, 15, 20, 30, and 50, so values up to 10 read without rounding. At about 17 series per histogram attribute set, the four histograms add about 3,400 series in the worst case, and the counters add about 2,000. Real use stays sparse, since no repository sees every combination.

Attributes never include a PR number, login, path, or review ID. Today the service labels nothing with those values, and D10 forbids the PR number and the login.

An operator would most want a reviewer model label, and that label breaks D10. On `github.pr.reviews`, a model label multiplies 600 by the number of models in use, and a fourth model would pass the cap. This design puts harness, model, and effort on the log line per review, where LogQL can count them without adding series. Open question 1 covers whether to change that.

## 11. Queries

These names assume the collector's translation from OpenTelemetry to Prometheus (unverified for this deployment). In that translation dots become `_`, and counters gain `_total`. A unit in braces drops, and `s` becomes `_seconds`. Quantiles from explicit buckets are estimates. `histogram_quantile(1, …)` returns the upper edge of the highest bucket with data, which can exceed the true largest value. Each query uses a 7-day window. Add `repository="…"` or `sum by (repository)` for a split by repository.

| Question | Query |
| --- | --- |
| Reviews per PR | `sum(increase(github_pr_reviews_total[7d])) / sum(increase(github_pr_review_first_reviews_total[7d]))` |
| Reviews per merged PR, median | `histogram_quantile(0.5, sum by (le) (increase(github_pr_review_reviews_per_merge_bucket[7d])))` |
| Reviews per merged PR, top | `histogram_quantile(1, sum by (le) (increase(github_pr_review_reviews_per_merge_bucket[7d])))` |
| Reviews posted, by state | `sum by (state) (increase(github_pr_reviews_total[7d]))` |
| Reviews dismissed | `sum by (cause) (increase(github_pr_review_dismissals_total[7d]))` |
| Findings per review, by priority | `sum by (priority) (increase(github_pr_review_findings_total[7d])) / scalar(sum(increase(github_pr_reviews_total[7d])) - sum(increase(github_pr_reviews_total{findings="unparsed"}[7d])))` |
| Findings per review, by kind | `sum by (kind) (increase(github_pr_review_findings_total[7d])) / scalar(sum(increase(github_pr_reviews_total[7d])) - sum(increase(github_pr_reviews_total{findings="unparsed"}[7d])))` |
| Share of first reviews that approve | `sum(increase(github_pr_review_first_reviews_total{state="approved"}[7d])) / sum(increase(github_pr_review_first_reviews_total[7d]))` |
| Approvals with findings | `sum(increase(github_pr_reviews_total{state="approved",findings="some"}[7d]))`. For the share, divide by `sum(increase(github_pr_reviews_total{state="approved"}[7d]))` |
| Approvals dismissed by a later push | `sum(increase(github_pr_review_dismissals_total{cause="stale_head"}[7d]))`. For the share, divide by approvals |
| Changes requested to next review | `histogram_quantile(0.5, sum by (le) (increase(github_pr_review_wait_after_changes_seconds_bucket[7d])))`. Use 0.95 for p95 |
| Findings per merged PR | `histogram_quantile(0.5, sum by (le) (increase(github_pr_review_findings_per_merge_bucket[7d])))`. For the mean, divide `sum(increase(github_pr_review_findings_per_merge_sum[7d]))` by `sum(increase(github_pr_review_findings_per_merge_count[7d]))` |
| Ready to first review | `histogram_quantile(0.5, sum by (le) (increase(github_pr_review_first_wait_seconds_bucket[7d])))`. Use 0.95 for p95 |
| Inline comments per finding | `sum(increase(github_pr_review_inline_comments_total[7d])) / sum(increase(github_pr_review_findings_total[7d]))` |
| Template drift | `sum(increase(github_pr_reviews_total{findings="unparsed"}[1h])) > 0` |

Reviews per PR divides reviews in the window by PRs first reviewed in the window. A PR with rounds on both sides of the window boundary skews it slightly. The per-merge histogram avoids that skew, and for this reason the design keeps both.

## 12. Tests

- **Parser.** The shared examples cover each section order the schema allows and from zero to eight findings. They also cover every priority and kind. Failure examples include a gap in numbering and a malformed bullet, along with a missing section. Further examples cover a `SUPERSEDED:` first line, the marker before and after the trailer, a missing trailer, CRLF endings, and a human review in free text. A fuzz test checks that `Parse` never panics and returns no body text apart from the trailer fields.
- **Example check in `factory/cli`.** `check_review` accepts every valid example and refuses every invalid one, so the examples can't drift from the schema.
- **Store.** Get, put, and delete on `review_prs`, a put that never moves a time backward, and retention that deletes records older than 30 days and keeps the rest.
- **Recorder.** For each instrument, a fake ledger and a manual reader check the gates and claim-before-record. The same tests cover `already_counted` on a second call, the absence of any observation after a claim error, the empty-body and PR-author rules, and each dismissal cause. Another test confirms that without a cached record the webhook path leaves `first_review` and `wait_after_changes` unclaimed. A final test asserts that no recorded attribute value comes from body text.
- **Webhook bodies.** Recorded `submitted`, `dismissed`, and `edited` deliveries, with titles and text replaced, serve as golden inputs to `Record`. The first live capture replaces the hand-built ones.
- **Repair walk with a fake client.** A fake `Getter` serves pages of reviews, events, and comments. Tests check that the walk records totals once per merged PR and fills context from the list. Others check that it clears per-PR tags, counts a dismissed review under `state="dismissed"`, and that a webhook after a repair leaves every series unchanged.

## 13. Pull request split

Each estimate counts added Go and Bazel lines including tests. All stay under 400. Example bodies under `testdata` fall outside the size gate.

| PR | Content | Estimate | Depends on |
| --- | --- | --- | --- |
| A | `internal/review` with `Parse`, the shared examples folder and its file group, and the Python example check | 330 | none |
| B | `review_prs` bucket with get, put, delete, and retention, plus appendix E's new row | 200 | none |
| C | `RecordReview` and the `Review` type, the counters and histograms for reviews, findings, first reviews, dismissals, and both waits, and the log line per review | 380 | A, and the interface from B |
| D | Webhook wiring in `Record` for the `pull_request_review` actions and the `pull_request` ready-time updates, with cache writes and the record deletion at `ended`. Also appendix B's `edited` change | 280 | C, with B merged |
| E | Open items from the walk, repair from the reviews list, ready time from events, and new counters for reconciliation | 360 | C |
| F | Per-merge totals in the walk and their two histograms | 220 | E |
| G | Architecture pages: the appendix A catalog and cardinality table and the telemetry content on page 8. Also the Grafana dashboard request for the platform repository | docs, no code | F |
| H | The review comments read and the inline counter, after inline posting merges | 200 | E, and the inline-posting change merged |

A and B can proceed in parallel. Work on C can start against fakes, and C merges after both. D and E both change the recorder's wiring, so E rebases after D. Deploying D before E turns on review repair lets live reviews claim first, and repairs then return `already_counted` in most cases. H waits for the review change that posts inline comments.

## 14. Open questions

1. **Reviewer model as a metric label.** This design puts harness, model, and effort on the log line per review and leaves them off metrics, which keeps D10 intact. A `model` label on `github.pr.reviews` alone fits about three models before the cap. Recommendation: keep it in logs for now. If dashboards need it, add one counter, `github.pr.review.by_model`, with `model`, `state`, and `findings` and without `repository`. That fits about 160 models, under an amendment to D10.
2. **History from before deploy.** The service can't export past weeks. The script's numbers for that period won't reappear. Suggested course: at deploy, run the script once and save its output as a dated report. Retire the script after the service has a week of data.
3. **Inline comments.** Slice H depends on a change still in progress. Proposed order: build A to G now and H after the inline change merges, so H targets the real comment shape.
4. **Per-merge totals without reconciliation.** The repair walk alone records the per-merge histograms, so a deployment without App credentials lacks them. Recommended: accept that, since reconciliation is the intended configuration. Whether production runs it today is unverified.
