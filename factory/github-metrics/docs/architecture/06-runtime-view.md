# 6. Runtime view

> Today versus design: the code has a webhook receiver and a GitHub client behind one request budget. The worker, the reconciler, the store, and the export remain design. Steps 3 to 6 are design. Steps 1 and 2 exist, and the receiver also checks `X-GitHub-Delivery` and `X-GitHub-Event` against fixed patterns. It answers `400` when either is missing or malformed, and otherwise logs the delivery and answers `202`.

## Scenario: a webhook delivery

1. GitHub posts a delivery through the tunnel to the receiver.
2. The receiver reads `X-Hub-Signature-256` and streams the body into the `HMAC`, capped at 2 MiB, and the result must match the header. Planned but absent: a method check that answers `405` and a media type check that answers `415`.
3. After the signature passes, one store transaction checks the dedupe table and records the delivery. In the same transaction, the receiver adds the body to the queue. A file sync follows, then the answer `202`. A delivery ID that repeats with a different body hash gets `400`. It logs the ID and both hashes.
4. Using the signed body, the worker finds the repository and PR number and marks the PR. It deletes the body in the same transaction. A body that lists `repositories_added` schedules a backfill of those repositories instead.
5. Next, the worker fetches the current PR state with conditional requests and compares it with what the service already exported.
6. Last, the worker exports newly completed spans. The store waits for the collector to accept them before recording their IDs.

Today the receiver answers `404` to every request except a [liveness probe](../../internal/receiver/receiver.go#L67) via `GET` and `POST /webhook`. After a valid signature it answers `202`, or `400` when `X-GitHub-Delivery` or `X-GitHub-Event` is missing or malformed, and it answers `413` over the cap. It then stops. Steps 3 to 6 remain to build, along with the `405` and `415` checks and the `400` for a repeated delivery ID.

## Scenario: scheduled reconciliation

Planned: every 15 minutes the reconciler lists each repository's PRs by update time with `If-None-Match` and walks back to the repository's watermark. Once a night it also reconciles every open PR. A first install triggers a backfill that produces traces and no metrics.

## Scenario: a failure

| Failure | Behavior |
| --- | --- |
| Bad signature | `401`, no store access |
| Body over 2 MiB | `413` |
| Store error or full disk | Planned: `503`. GitHub records a failed delivery and reconciliation heals it |
| Collector down | Planned: the failed export leaves the store unchanged and the PR retries on the next pass |
| Secondary rate limit | The client backs the budget off up to 15 minutes and retries |
