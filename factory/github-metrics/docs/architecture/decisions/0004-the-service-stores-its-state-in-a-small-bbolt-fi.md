# D4. The service stores its state in a small `bbolt` file on a mounted volume

- **Status:** accepted, 2026-11-14.
- **Rule:** the service writes its whole state to one `bbolt` file on a writable volume. That covers the queue, the dedupe table, the exported span IDs, the metric milestones, the watermarks, the entity tags, and the store's creation time. The volume is the container’s one writable location. Restoring an older copy of the file is forbidden, and the operator deletes a damaged file so the service starts empty.
- **Undo:** swap the store behind its interface, such as for SQLite. Losing the file re-exports every span of every PR in scope once, through a fresh backfill, and the creation time keeps metrics from doubling.
