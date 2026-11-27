# D5. Child spans export when complete, and the root exports once at the first merge or close

- **Status:** accepted, 2026-11-14.
- **Rule:** each child span goes out once its end time exists. The service exports the root once, at the PR’s first terminal state. A reopen never triggers another root export. After a reopen, the reopen, later closes and merges, and later root events become child spans, and the collector pipeline doesn't tail-sample.
- **Undo:** delay every span until the terminal state, which hides open PRs from the backend.
