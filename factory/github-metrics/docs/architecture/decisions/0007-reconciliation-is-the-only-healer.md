# D7. Reconciliation is the only healer

- **Status:** accepted, 2026-11-14.
- **Rule:** the first release doesn't call GitHub's redelivery API. A 15-minute incremental run and a nightly pass over open PRs heal missed deliveries, which works because no telemetry depends on a webhook-only fact.
- **Undo:** add a redelivery client that signs a JSON Web Token (JWT) with the App key.
