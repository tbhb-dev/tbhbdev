# D6. Webhooks answer 2xx after verification, dedupe, and a synced enqueue

- **Status:** accepted, 2026-11-14.
- **Rule:** the receiver verifies the signature before accessing the dedupe table. One transaction drops a known body hash, rejects a known delivery ID with a different body hash, and otherwise records both and enqueues the body, and the receiver answers `202` after the sync. A delivery serves solely to trigger a reconcile, and a delivery the worker can't parse moves to a dead-letter bucket after five tries.
- **Undo:** answer after an in-memory enqueue, which trades durability for latency.
