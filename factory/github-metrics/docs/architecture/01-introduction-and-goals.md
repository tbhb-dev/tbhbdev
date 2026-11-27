# 1. Introduction and goals

## Overview

Today nobody can see how long a pull request (PR) waits for review or merge, or view one PR's lifecycle as a timeline. `github-metrics` fills that gap. In the design it reads PR activity from GitHub and publishes OpenTelemetry (OTel) data. A PR lifecycle becomes one trace, each repository gets three timing histograms, and the design has the service export its own logs. The receiver and its signature check exist today.

| Goal | Meaning |
| --- | --- |
| One trace per PR | The trace runs from creation to the first merge or close, with child spans for commits, review requests, reviews, reopens, and closes |
| Timing metrics | Time from opening a pull request to its first review, and separately to its approval and its merge. Each has a repository label and an author type label |
| No missed activity | Planned: scheduled reconciliation heals every missed webhook |
| No private content | Design target: titles, bodies, comment text, and diffs never leave the service. The receiver checks the two header values it logs (page 8) |

## Quality goals

These goals describe the finished service. The code is far from most of them.

| Goal | Scenario |
| --- | --- |
| Correctness | GitHub sends a delivery twice, or never. The service exports the same spans and metrics as if it had arrived once. |
| Restraint | The design lets a backfill run while webhooks arrive. Requests to GitHub stay under the shared budget, and webhook-driven work goes first. |
| Safety | A request arrives with a bad signature. The service answers `401` and leaves its state unchanged. |
