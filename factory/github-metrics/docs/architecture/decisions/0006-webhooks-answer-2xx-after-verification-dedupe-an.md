# D6. Webhooks answer 2xx after verification, dedupe, and a synced enqueue

- **Status:** accepted, 2026-11-14.
- **Rule:** the receiver touches the dedupe table only after it verifies the signature. One transaction drops a known body hash, rejects a known delivery ID with a different body hash, and otherwise records both and enqueues the body, and the receiver answers `202` after the sync. A delivery only triggers a reconcile, and a delivery the worker can't parse moves to a dead-letter bucket after five tries.
- **Undo:** answer after an in-memory enqueue, which trades durability for latency.
