# D8. The service reads GitHub through REST with conditional requests and one shared budget

- **Status:** accepted, 2026-11-14.
- **Rule:** the service calls the REST API with `If-None-Match`. The target spaces request starts at least 250 milliseconds apart, which doesn't serialize requests in flight. One shared token bucket, refilled at 2,500 requests an hour with at most 60 tokens held, serves webhook-triggered reconciles first, then incremental runs, the nightly sweep, and the backfill.
- **Undo:** move the per-PR fetch to one GraphQL query, as appendix B of the original design describes.
