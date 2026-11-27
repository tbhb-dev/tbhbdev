# D7. Reconciliation alone heals missed deliveries

- **Status:** accepted, 2026-11-14.
- **Rule:** the first release doesn't call GitHub's redelivery API. A 15-minute incremental run and a nightly pass over open PRs heal missed deliveries, which works because the REST API exposes every fact the telemetry needs.
- **Undo:** add a redelivery client that signs a JSON Web Token (JWT) with the App key.
