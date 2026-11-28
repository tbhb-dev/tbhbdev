# Appendix E: budget, dedupe, and retention

Today the repository has a webhook receiver, a request budget, and a GitHub client. Everything else on this page is design or planned, and every estimate here is unmeasured until the first run.

Pages 6, 7, and 8 summarize details that this appendix brings over from the original design in the `drafts/github-metrics` folder of the `tbhb-dev/wiki` repository at commit `8e06fb1`. The “Request budget” section comes from appendix D of that design. The remaining sections draw from its webhook security, data, and failure handling sections.

## Request budget

An App installation gets at least 5,000 requests an hour from GitHub, and 50 more per repository once it has more than 20, up to 12,500. Nobody has checked whether that counts all repositories or excludes the first 20 (unverified). The budget here uses the 5,000 floor.

List endpoints return at most 100 items a page. Each per-PR fetch of reviews, commits, and timeline averages 4 requests in the estimates, not 3, because about a third of the lists need a second page. A PR with more than 100 commits or timeline events costs more (unverified until the first run measures it).

Most repositories need one PR list page per incremental run, since the walk stops at the watermark, and the estimate adds 4 pages for busy ones.

| Work | Requests | Per hour, worst case with no `304` |
| --- | --- | --- |
| Incremental run: a repository list plus one PR list page per repository, with 4 extra pages | 45 | 180 |
| PRs changed, about 30 an hour: reviews, commits, and timeline with pagination | 4 each | 120 |
| Webhook reconciles, about 30 PRs an hour: the PR, then reviews, commits, and timeline | 5 each | 150 |
| Total steady state | | 450, about 9% of 5,000 |

The nightly sweep adds about 400 requests for 100 open PRs. Most return `304`, and GitHub doesn't count a conditional request that returns `304` against the primary limit when it includes an `Authorization` header (existing, per appendix C). The real cost is well below the worst case. Whether `ETag` values change each time the token rotates, once an hour is unverified, and the worst-case figures assume they do.

### Shared bucket

One bucket serves the backfill, the nightly sweep, the incremental runs, and webhook-triggered reconciles. Each request takes a token from it. Tokens arrive one every 1.44 seconds, which is 2,500 an hour and half of the 5,000 floor, and the bucket caps at 60 tokens. Any rolling hour then has at most 2,560 requests. Even a `304` answer takes a token.

Each new token goes to the highest-priority waiter, in this order: webhook-triggered reconciles, incremental runs, the nightly sweep, and the backfill. There's always backfill work waiting, but it takes leftover tokens. Even when the backfill spends the hour's allotment, webhook work can use the next token.

That leaves the backfill about 2,500 - 450 = 2,050 requests in an ordinary hour, and about 2,500 - 450 - 400 = 1,650 in the hour of the nightly sweep. The service also reads `x-ratelimit-remaining` on every response and stops all work until `x-ratelimit-reset` when fewer than 500 remain. That covers any other use of the installation's limit.

### Pacing

GitHub's REST secondary limit is 900 points a minute, and most `GET` requests cost 1 point. Serial requests alone don't bound points per minute. The limiter spaces requests at least 250 milliseconds apart, even with a full bucket, which allows at most 240 requests and 240 points in any minute. Whether a `304` costs a secondary-limit point is unverified. The pacer spaces it like any other request.

### Backfill duration

Take 40 repositories of about 300 PRs each. A backfill reads 12,000 PRs, which is 120 list pages plus about 48,000 per-PR requests at 4 each. At about 2,050 requests an hour that takes about 24 hours. Meanwhile the bucket holds use to about 2,560 in any rolling hour.

A backfill writes spans with their original timestamps and records metric milestones as seen, without histogram observations. The PR commits endpoint returns at most 250 commits. For a PR with more than 250, the backfill stops after writing spans for its first 250 commits.

## Dedupe and replay rules

GitHub signs the body and leaves the headers unsigned. Changes to `X-GitHub-Delivery` and `X-GitHub-Event` leave the signature valid. The design handles that with these rules, applied in the same transaction as the enqueue.

- **Authenticate first.** A request that fails the signature doesn't read or write the dedupe table.
- **Body hash first.** The receiver hashes the body with the 256-bit Secure Hash Algorithm (SHA). If the SHA-256 digest is in the table, the receiver drops the delivery and answers `202` regardless of the delivery ID.
- **Delivery ID second.** If the delivery ID is already in the table with a different body hash, the receiver rejects the request with `400` and logs the ID and both hashes.
- **Bound.** Entries last 7 days, and the table holds at most 200,000 entries, evicting the oldest first.
- **Older replays.** A signed body older than the table's window can arrive again. It can trigger reconciliation or an idempotent backfill and has no other effect. That's safe while the span and milestone state is intact. After a store loss it costs no more than the backfill that the loss already triggers.
- **Event header.** The worker reads the repository, the PR, and any `repositories_added` list from the signed body, and reserves `X-GitHub-Event` for log lines.

Today `main` has none of the table rules. It checks that both headers match fixed patterns before logging them (page 8), and it doesn't record past deliveries.

## Secrets and routing

During webhook secret rotation, the design accepts signatures made with either the current or the previous secret. The App private key and the webhook secret come from 1Password through the deploy host's secret manager, mounted as files with read permissions and no write permissions. Neither is in the image or the environment.

In the design, the public tunnel routes `/webhook`. At the edge, every other path gets `404`. A second listener serves health checks, and the tunnel doesn't route it. Today `main` serves `/healthz` and `/webhook` from one handler, and page 7 doesn't show a second listener. Nobody has verified the route from this repository.

## Data kept

| Data | Where | Kept for |
| --- | --- | --- |
| Raw webhook bodies with titles and review text | The queue in the store | Until the worker extracts the repository and PR, a matter of seconds under normal conditions. The worker then removes the entry from the queue |
| Raw bodies that fail to parse | The dead-letter bucket | 7 days, then deleted |
| Delivery IDs, body hashes, and times | The dedupe table | 7 days, at most 200,000 entries |
| Span IDs, milestones, watermarks, and `ETag` values | The store | The life of the store |
| PR node IDs and their ready and update times | The `review_prs` bucket in the store | Until the worker claims the `ended` milestone or the daily pass finds an update time older than 30 days |

Deletion from the store is logical. `bbolt` may not zero freed pages, so a deleted body can stay in the file and in any copy of it until `bbolt` reuses its pages (unverified). The design proposes mode `0600` for the store file, owned by the image's non-root user, on a volume reserved for this service to mount. The threat-model follow-up, [plan step 6 on page 11](11-risks-and-technical-debt.md), settles the final modes, owner, and corruption handling.

## Related pages

- [Page 6, runtime view](06-runtime-view.md) has the delivery, reconciliation, and failure scenarios.
- [Page 8, cross-cutting concepts](08-crosscutting-concepts.md) has the short budget and security summaries.
- [Appendix C](appendix-c-github-limit-evidence.md) has the sources and labels for the GitHub limits used here.
