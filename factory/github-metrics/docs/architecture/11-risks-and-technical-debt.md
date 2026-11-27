# 11. Risks and technical debt

| Risk or debt | Effect | Handling | Evidence |
| --- | --- | --- | --- |
| The backend hides spans dated weeks in the past | Old PR traces don't appear in search | The first export spike sends a backdated trace and searches for it | Unverified |
| Trace retention is shorter than a PR's life | The first child spans age out before the root arrives | Set retention past the longest expected PR, and report orphans | Unverified |
| The OTel Go SDK can't set chosen span IDs | The trace model needs another export path | A custom ID generator, with direct export to the collector as the fallback | Unverified |
| The collector's log path differs from the plan | Logs don't reach Loki | The first export spike sends a log record and searches for it, with standard output as the fallback | Unverified |
| A body of up to 2 MiB reaches the queue | Disk and queue size grow | Planned: spool to a bounded temporary file and verify the signature before the append, with a proposed cap of four bodies in flight | New |
| Deleted bodies remain in the `bbolt` file | Raw bodies persist physically until `bbolt` reuses the pages, even after the queue marks them deleted | Proposed: mode `0600` and a dedicated volume. Whether to back up the queue is open, and plan step 6 decides it | Unverified |
| The client stores an `ETag` on receipt | A 200 body the reconciler can't use leaves a stale tag | The planned reconciler clears the tag in that case | New |
| `ETag` values change when the installation token rotates | Fewer free `304` answers | The worst-case budget already assumes every request returns a full response | New (appendix E) |
| An outage of more than 24 hours | Milestones from the outage get traces but no metric observations | Accepted, since the traces still record each duration | New |
| A crash between a span export and its store write | That reconcile run's spans export twice | Accepted, since the window is one write long | New |
| A crash during a collector outage | The service loses every metric observation since the last accepted export | Accepted, since the worker commits milestones first and they never double | New |
| The service loses its store, or someone restores an older snapshot | Every span re-exports, and a restore also double-counts recent milestones | Metrics require a milestone that occurred after the service created the store. Operators must never restore a snapshot | New (appendix A) |
| Unsigned traffic floods the public webhook path | CPU spent on rejected requests | Size cap before reading, `HMAC` before parsing, and an optional edge rate limit | New |
| Receiver gaps: rejection of missing or malformed signatures and headers, read, write, and header timeouts, connection and concurrency limits, unknown event names, and oversize valid deliveries | Resource exhaustion or silent drops | Gates of the threat-model follow-up, plan step 6 | New, deferred to step 6 |
| Tunnel acceptance tests don't cover the security gates | A route wider than `POST /webhook` passes the tests | Gates of the threat-model follow-up, plan step 6 | New, deferred to step 6 |
| Secret files and the store have loose modes or ownership, or the store is corrupt or has insecure backups | A local reader gets the key or queued bodies, or the service loses its record | Gates of the threat-model follow-up, plan step 6 | New, deferred to step 6 |
| The operator never retires the previous webhook secret or never rotates the App key | An old credential keeps working | Gates of the threat-model follow-up, plan step 6 | New, deferred to step 6 |
| Backend access and retention for private-repository names and logins | Wider exposure than the operator intends | The operator confirms it in the threat-model follow-up | Unverified |

Tech debt: CI history backfill caps run and job pages without a cursor to continue the walk. A crash after a durable claim can lose a log. Backfilled claims also suppress later live timing metrics. The one-off Loki pass accepts these limits.

## Plan

Step 1 finished. Steps 2, 3, and 5 to 8 remain open work. Today `main` has the receiver, the request budget, and the GitHub client, which are parts of steps 3 and 5.

| Step | What lands | Done when | Owner |
| --- | --- | --- | --- |
| 1 | The operator answers D1 and D2 | Finished: the operator accepted [D1](decisions/0001-may-tbhb-dev-create-the-read-only-github-app-and.md) and [D2](decisions/0002-may-the-webhook-path-face-the-public-internet-th.md) on 2026-11-14 | Operator |
| 2 | A spike exports one backdated, deterministic trace to the collector | The trace appears in the backend under its derived ID, with GitHub's timestamps | Platform engineering |
| 3 | The service repository with the receiver, store, worker, and tests | Tests pass for a replayed delivery, a duplicate, a reused delivery ID with a different body, an out-of-order pair, and a bad signature | Platform engineering |
| 4 | Dropped: the operator's decision replaced the earlier organization collector design, so there is no boundary to confirm | [D13](decisions/0013-the-boundary-with-the-organization-collector-pen.md) records the supersession | Platform engineering |
| 5 | The reconciler, the shared request budget, and the backfill | A test against recorded responses shows a full run under its request budget and pacing | Platform engineering |
| 6 | A threat-model follow-up design covers the receiver gaps, the tunnel acceptance gates, secret and store file modes, store backup and corruption, secret retirement and key rotation, and backend access and retention | That design merges with each gate specified, and the operator confirms the backend input | Platform engineering |
| 7 | The App from its manifest, the secrets in 1Password, and the host operator's [deployment of the service binary](07-deployment-view.md) with the tunnel | Step 6 has merged, a test delivery from the App's settings page returns `202`, and a public `GET` on any path returns `404` | Platform engineering and the host operator |
| 8 | Mimir recording rules and a Grafana dashboard for the direct metrics, including the created-to-merged goal and the cycle time, which replace the scripted timing table. Open item, unverified: whether Mimir ingests native histograms. The owner checks the setting before the exponential histograms go live, and otherwise uses the explicit-bucket fallback | The dashboard shows each repository's 24-hour median and p95, and the goal flag, from live data | Platform engineering |

The first deploy, step 7, waits on step 6.
