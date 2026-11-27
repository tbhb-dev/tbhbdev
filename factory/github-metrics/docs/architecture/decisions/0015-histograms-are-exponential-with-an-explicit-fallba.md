# D15. Histograms are exponential, with an explicit fallback

- **Status:** accepted, 2026-11-14.
- **Rule:** every duration and size histogram uses the OTel exponential (native) histogram aggregation, so Mimir can compute the 10-minute median and 15-minute p95 goal with a bounded relative error of about 9%. If the backend can't load native histograms, the same instruments use explicit boundaries of 60, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400, 172800, 345600, 604800, 1209600, and 2592000 seconds, where 600 and 900 are goal edges. Size histograms need their own boundaries in that case.
- **Undo:** switch the aggregation with an SDK view. Old series stop, and new ones start empty.
