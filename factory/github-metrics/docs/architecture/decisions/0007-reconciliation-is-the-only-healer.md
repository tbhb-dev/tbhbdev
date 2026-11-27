# D7. Reconciliation repairs missed deliveries without redelivery

- **Status:** accepted, 2026-11-14.
- **Rule:** the first release doesn't call GitHub's redelivery API. A 15-minute incremental run and a nightly pass over open PRs heal missed deliveries, which works because no telemetry depends on facts exclusive to webhook payloads.
- **Undo:** add a redelivery client that signs a JSON Web Token (JWT) with the App key.
