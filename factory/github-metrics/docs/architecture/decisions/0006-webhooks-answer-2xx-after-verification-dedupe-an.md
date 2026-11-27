# D6. Webhooks answer 2xx after verification, dedupe, and a synced enqueue

- **Status:** accepted, 2026-11-14.
- **Rule:** the receiver verifies the signature before it can access the dedupe table. One transaction drops a known body hash, rejects a known delivery ID with a different body hash, and otherwise records both and enqueues the body, and the receiver answers `202` after the sync. A delivery triggers reconciliation without supplying telemetry content. If the worker can't parse the delivery after five tries, it moves the delivery to a dead-letter bucket.
- **Undo:** answer after an in-memory enqueue, which trades durability for latency.
