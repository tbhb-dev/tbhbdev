# 10. Quality requirements

> Today versus design: these are targets. The code can't yet meet most of them.

| Quality | Scenario | Measure |
| --- | --- | --- |
| Correctness | A delivery arrives twice, late, or never | The reconciled telemetry is the same in all three cases |
| Efficiency | Steady state with about 50 repositories | At most about 450 requests an hour, 9% of the 5,000 floor |
| Memory | An unauthenticated client posts a body larger than the 2 MiB cap | A missing or malformed signature gets 401 first. Otherwise the receiver reads about 2 MiB (one detection byte over) and answers 413, and at most 16 bodies are read at once |
| Durability | The service crashes after answering `202` | The delivery is in the synced queue |
| Cardinality | Many repositories and authors | At most 2,150 metric series with native histograms and 25,950 with the explicit fallback, capped by an SDK limit of 2,000 attribute sets per instrument |
| Recovery | A lost store | The service exports each span once more and doesn't double count metrics. Restoring an older snapshot is forbidden |
