# 9. Architecture decisions

Each decision has its own record under `decisions/`, with its rule, how to undo it, and its status. A rule states the decided design. Today the code covers only the receiver, the request budget, and the GitHub client, and the live App and tunnel route are the operator's to verify.

| Record | Decision |
| --- | --- |
| [D1](decisions/0001-may-tbhb-dev-create-the-read-only-github-app-and.md) | May tbhb-dev create the read-only GitHub App and install it on all repositories? |
| [D2](decisions/0002-may-the-webhook-path-face-the-public-internet-th.md) | May the webhook path face the public internet through a Cloudflare Tunnel at `github-metrics.tbhb.dev`, and may the image be public? |
| [D3](decisions/0003-trace-and-span-ids-derive-from-the-pr-s-graphql.md) | Trace and span IDs derive from the PR's GraphQL node ID |
| [D4](decisions/0004-the-service-stores-its-state-in-a-small-bbolt-fi.md) | The service stores its state in a small `bbolt` file on a mounted volume |
| [D5](decisions/0005-child-spans-export-when-complete-and-the-root-ex.md) | Child spans export when complete, and the root exports once at the first merge or close |
| [D6](decisions/0006-webhooks-answer-2xx-after-verification-dedupe-an.md) | Webhooks answer 2xx after verification, dedupe, and a synced enqueue |
| [D7](decisions/0007-reconciliation-is-the-only-healer.md) | Reconciliation is the only healer |
| [D8](decisions/0008-the-service-reads-github-through-rest-with-condi.md) | The service reads GitHub through REST with conditional requests and one shared budget |
| [D9](decisions/0009-export-over-http-with-protobuf-to-the-host-s-col.md) | Export over HTTP with protobuf to the host's collector |
| [D10](decisions/0010-repository-and-author-type-are-the-only-metric-a.md) | Repository and author type are the only metric attributes |
| [D11](decisions/0011-checks-issue-comments-pushes-and-review-threads.md) | Checks, issue comments, pushes, and review threads stay out of the first release |
| [D12](decisions/0012-telemetry-contains-no-pr-titles-bodies-comment-t.md) | Telemetry contains no PR titles, bodies, comment text, or diffs |
| [D13](decisions/0013-the-boundary-with-the-organization-collector-pen.md) | The boundary with the organization collector (superseded) |
| [D14](decisions/0014-the-service-records-every-duration-itself-not-from-s.md) | The service records every duration itself, not from spans |
| [D15](decisions/0015-histograms-are-exponential-with-an-explicit-fallba.md) | Histograms are exponential, with an explicit fallback |
