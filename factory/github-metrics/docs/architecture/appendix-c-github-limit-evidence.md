# Appendix C: GitHub limit evidence

Today the repository has a webhook receiver, a request budget, and a GitHub client. Everything else on this page that describes the service is design or planned.

This page moves the evidence tables out of the original design, appendix A of the `drafts/github-metrics` folder in the `tbhb-dev/wiki` repository at commit `8e06fb1`. Each claim has one label. Existing means a primary source states it, and the design's author read that source on 2026-11-13. New means the design proposes it. Unverified means nobody has checked it yet. Superseded means a later operator decision made the claim obsolete. The tables keep every quantity from the design. Beyond that, the page splits the design's GitHub section into six sections and reorders rows. It renames the last section from This design to Design assumptions and adds short intros to three sections, on rate limits, webhook delivery, and telemetry libraries. It also rewords the claim text of two rows, the OTel all-zero ID row and the organization collector row. Most source changes are rewording or added links. In the permission keys row, the source now says the manifest flow hasn't confirmed the keys and the label went from Existing to New. In the organization collector row, the source now cites the operator decision and the label went from Unverified to Superseded.

## GitHub documentation pages

Each page resolved on 2026-11-13. A resolving page proves only that the page exists, and the claims tables show which claims the author checked against it.

| Short name | URL |
| --- | --- |
| Webhook events and payloads | <https://docs.github.com/en/webhooks/webhook-events-and-payloads> |
| Validating webhook deliveries | <https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries> |
| Handling failed webhook deliveries | <https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries> |
| Permissions required for GitHub Apps | <https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps> |
| Rate limits for the REST API | <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api> |
| Best practices for using the REST API | <https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api> |
| Issue event types | <https://docs.github.com/en/rest/using-the-rest-api/issue-event-types> |
| Timeline endpoints | <https://docs.github.com/en/rest/issues/timeline> |
| Pull request endpoints | <https://docs.github.com/en/rest/pulls/pulls> |
| Review comment endpoints | <https://docs.github.com/en/rest/pulls/comments> |
| GitHub App webhook endpoints | <https://docs.github.com/en/rest/apps/webhooks> |
| GitHub App endpoints | <https://docs.github.com/en/rest/apps/apps> |
| Generating a JSON Web Token (JWT) | <https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app> |
| Generating an installation token | <https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app> |
| Registering an App from a manifest | <https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest> |

## Rate limits and secondary limits

The design sizes its request budget from these figures. The budget in `internal/budget` exists today, and the design plans a reconciler to spend it.

| Claim | Label | Source |
| --- | --- | --- |
| An installation gets at least 5,000 requests an hour, 50 more per repository once it has more than 20, and at most 12,500 | Existing | Rate limits for the REST API |
| Secondary limits include 100 concurrent requests and 900 points a minute for REST, and a client waits for `retry-after` or at least one minute | Existing, 900 points re-read 2026-11-13 | Rate limits for the REST API |
| Most REST `GET`, `HEAD`, and `OPTIONS` requests cost 1 secondary-limit point | Existing, re-read 2026-11-13 | Rate limits for the REST API |
| A `304` answer costs no secondary-limit point | Unverified | The design's pacer counts it anyway |
| A conditional request that returns `304` doesn't count against the primary limit when it includes an `Authorization` header | Existing | Best practices for using the REST API |
| GitHub advises serial requests to avoid secondary limits | Existing | Best practices for using the REST API |
| List endpoints return at most 100 items a page | Existing, re-read 2026-11-13 | Pull request endpoints, `per_page` |
| The PR commits endpoint returns at most 250 commits | Existing, re-read 2026-11-13 | Pull request endpoints |
| About a third of per-PR lists need a second page | Unverified | An assumption for the budget, and the design's first run would measure it |
| `ETag` values remain valid across the hourly token rotation | Unverified | The first live run after plan step 7 on [page 11](11-risks-and-technical-debt.md) would measure the `304` rate |

## Webhook delivery and payload limits

The receiver exists today. The design plans the rest of its delivery handling.

| Claim | Label | Source |
| --- | --- | --- |
| GitHub caps webhook payloads at 25 MB | Existing, re-read 2026-11-13 | Webhook events and payloads |
| The webhook signature is a Hash-based Message Authentication Code (HMAC) over the payload contents only, so headers such as `X-GitHub-Delivery` aren't covered | Existing, re-read 2026-11-13 | Validating webhook deliveries |
| GitHub doesn't redeliver failed deliveries automatically | Existing | Handling failed webhook deliveries |
| Listing and redelivering App webhook deliveries needs a JSON Web Token (JWT) | Existing | GitHub App webhook endpoints |
| A redelivery keeps its original `X-GitHub-Delivery` value | Unverified | The docs read for the design don't say |
| Every App receives `installation` and `installation_repositories` by default | Existing | Webhook events and payloads |

## Permissions and events

| Claim | Label | Source |
| --- | --- | --- |
| The `pull_request`, `pull_request_review`, `pull_request_review_comment`, and `pull_request_review_thread` events each need Pull requests read | Existing | Webhook events and payloads |
| `issue_comment` needs Issues read, `check_run` and `check_suite` need Checks read, and `push` needs Contents read | Existing | Webhook events and payloads |
| Listing pull requests (PRs), a PR's commits, its reviews, and its requested reviewers needs Pull requests read | Existing | Permissions required for GitHub Apps |
| The PR files endpoint is under Pull requests read | Existing, re-read 2026-11-13 | Permissions required for GitHub Apps |
| The issue timeline endpoint accepts either Issues read or Pull requests read | Existing | Permissions required for GitHub Apps |
| The manifest keys are `default_permissions` and `default_events` | Existing | Registering an App from a manifest |
| The permission keys are `pull_requests` and `metadata` | New | GitHub App endpoints, "Create an installation access token for an app." Its `permissions` body uses these names. The manifest flow in plan step 7 on [page 11](11-risks-and-technical-debt.md) hasn't run yet, so it doesn't confirm them |

## Payload content and timeline events

| Claim | Label | Source |
| --- | --- | --- |
| A `pull_request_review_thread` payload doesn't include a GitHub timestamp for the `resolved` or `unresolved` action itself | Unverified | Webhook events and payloads, not re-read for this field |
| The PR endpoint can return a diff through the `application/vnd.github.diff` media type | Existing, re-read 2026-11-13 | Pull request endpoints |
| Timeline event types include `review_requested`, `ready_for_review`, `convert_to_draft`, `head_ref_force_pushed`, and `reopened` | Existing, re-read 2026-11-13 | Issue event types |
| A timeline `review_requested` event has `id`, `node_id`, and `created_at` | Existing | Issue event types |
| The REST review comment endpoints and timeline event types don't expose thread resolution state, and review comments include `diff_hunk` | Existing, re-read 2026-11-13: neither page lists a resolved field or event | Review comment endpoints, Issue event types |

## Authentication tokens

| Claim | Label | Source |
| --- | --- | --- |
| A JWT expires at most 10 minutes ahead, uses RS256, and should set `iat` 60 seconds in the past | Existing | Generating a JSON Web Token (JWT) |
| An installation token lasts one hour, and its request can narrow its permissions | Existing | Generating an installation token |

## Storage

| Claim | Label | Source |
| --- | --- | --- |
| `bbolt` doesn't zero freed pages, so logically deleted bodies can remain in the file | Unverified | Plan step 6 on [page 11](11-risks-and-technical-debt.md) covers file modes and backups |

## Telemetry libraries and backends

This section concerns the planned export path. The code has no OpenTelemetry (OTel) export today.

| Claim | Label | Source |
| --- | --- | --- |
| OTel treats an all-zero trace or span ID as invalid | Existing | W3C Trace Context and the OTel trace API specification |
| The Go SDK accepts explicit start and end timestamps on spans | Existing, not re-read for this draft | OTel Go `trace.WithTimestamp` option |
| A custom ID generator in the Go SDK can return the derived IDs | Unverified | Plan step 2 on [page 11](11-risks-and-technical-debt.md) |
| The `vcs.*` attribute names match the current semantic conventions | Unverified | The conventions are in development, and [step 3](11-risks-and-technical-debt.md) pins a version |
| The backend shows and searches spans dated weeks in the past | Unverified | Plan step 2 on [page 11](11-risks-and-technical-debt.md) |
| Backends keep or drop a second span with an already-exported ID | Unverified | Nobody has tested it, and the design avoids re-exporting the root |
| The host collector accepts OTel Protocol (OTLP) over HTTP on port 4318 | Unverified | Port 4318 is the OTLP default, and [step 2](11-risks-and-technical-debt.md) confirms it on the host |
| Who can read the backends, and how long they keep this telemetry | Unverified | The operator confirms it in the threat-model follow-up, [plan step 6](11-risks-and-technical-debt.md) |

## Design assumptions

| Claim | Label | Source |
| --- | --- | --- |
| tbhb-dev has tens of repositories, about 40 | Unverified | An assumption for the budget, and the first run would count them |
| About 30 PRs change an hour at peak | Unverified | An assumption for the budget |
| An earlier organization collector design covered continuous integration (CI), Actions, and organization-level counters | Superseded | On 2026-11-14 the operator decided that this design replaces it, so this design doesn't assume that another service emits these metrics |
| The architecture diagrams use Structurizr | Existing | The [architecture index](README.md#diagrams) documents how to render the SVGs |
