# D13. The boundary with the organization collector (superseded)

- **Status:** superseded, 2026-11-14. Because the operator decided that this design replaces the earlier organization collector design, no collector owner remains to confirm a boundary, though CI, Actions, and organization-level counters stay outside this design today and this design doesn't assume that another service emits these PR metrics. The text below is the original record.
- **Original status:** accepted, 2026-11-14, by platform engineering, as an unverified assumption about the other design. Its owner hasn't confirmed it.
- **Rule:** `github-metrics` covers PRs, reviews, review requests, and commits on PRs. The organization collector covers CI, Actions, and organization-level counters, and each uses its own App or token.
- **Undo:** the other design's owner moves the line, and this design follows.
