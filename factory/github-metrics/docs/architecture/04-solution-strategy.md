# 4. Solution strategy

> Today versus design: the table states targets. The code has only the receiver, the request budget, and the GitHub client.

| Goal | Approach |
| --- | --- |
| Telemetry doesn't depend on a delivery | The design has a webhook trigger a reconcile, with telemetry drawn from REST responses that show the PR's current state. No code does this yet |
| Missed deliveries heal | Planned: a 15-minute incremental run and a nightly pass over open PRs reconcile the same way a delivery does |
| Replays and reruns are safe | Design: trace and span IDs derive from a hash of the PR's node ID, and every run produces the same IDs. The store keeps the exported span IDs |
| Spans have real times | Design: span times come from GitHub's own timestamps |
| Metrics count once | Design: the worker commits each milestone to the store before it records the observation |
| Bounded request volume | One shared token bucket serves every caller in priority order, and conditional requests make unchanged answers free |
| Bounded memory | The receiver streams each body into the signature hash and never buffers it |
| Logs follow the standard path | The design exports logs to the same collector endpoint as traces and metrics, and also writes JSON to standard output. Today only the JSON output exists |
