---
description: Per-commit identity trailer and signature checks over a range.
generated:
  - role: generated
    harness: codex:0.166.0
    model: openai:gpt-6.2-sol
    surface: exec
    session: sha-256-64:5b60eb8c0a5ee790
    at: "2026-11-27T03:00:36Z"
status: stable
tags: [alpha]
title: Verify range
type: Reference
---

# Verify range

`tbhbdev alpha verify-range BASE TIP [-C PATH] [--identity-only]` checks each commit in `BASE..TIP`, oldest first. Use `-C PATH` to read commits from the repository at that path.
Exit codes are 0 when every commit passes, 1 when any fails and 2 for usage or Git errors.

## Default mode

For a commit with one parent, the agent must be both the author and the committer. The commit needs a signature header and valid attribution trailers.
For a merge commit, the command checks the signature header and walks each parent.
Root commits fail.
Sign-off trailers, co-author trailers and GitHub's no-reply addresses fail it.

## Identity trailer checks

With `--identity-only`, the command checks the identity trailers of every non-merge commit in the range. It reports full commit hashes. Signatures are optional.

- A commit with the agent email as author or committer requires a valid `Generated-by` trailer.
- Other commits require a valid `Generated-by` or `Committed-by` trailer.
- Every `Committed-by` trailer must match the name and email Git records for the committer.
- Follow the [grammar for identity trailers](identity.md) when writing `Generated-by` and `Assisted-by`. Put `Generated-by` before `Assisted-by`. An `Assisted-by` trailer alone fails.
