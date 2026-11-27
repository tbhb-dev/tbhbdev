---
description: Export, lint, and refresh committed Structurizr workspace diagrams.
status: stable
tags: [architecture, bazel]
title: Structurizr tooling
type: Readme
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:4a647161bc55662a
    at: "2026-11-27T18:47:36Z"
---

# Structurizr tooling

Load `structurizr_workspace` from `@tbhb_rules_structurizr//:defs.bzl`.

```starlark
structurizr_workspace(
    name = "architecture",
    dsl = "docs/architecture/workspace.dsl",
    workspace_json = "docs/architecture/workspace.json",
    diagrams_dir = "docs/architecture/diagrams",
)
```

Run `bazel run :architecture` to update committed exports and `bazel test :architecture_test` to check validation, inspection, and freshness. `:architecture.renders` contains JSON, PlantUML text, SVG and PNG exports.

The pinned tools render with a headless Java runtime and Smetana. They use the four IBM Plex Sans faces bundled in `fonts/` under the SIL Open Font License 1.1. On Linux, a generated font configuration lists only the bundled font directory and a scratch cache, and rendering requires `libfontconfig` and `libfreetype`. On macOS the JVM uses its native font manager. The renderer checks each bundled TrueType file through the JVM’s fontconfig mappings or registration on Linux and through registration on macOS. Each SVG must specify IBM Plex Sans. The stubbed JVM test exercises those checks on both paths.

The freshness test compares JSON and SVG bytes. The PNG test compares bytes on Linux x86_64, where the update target writes PNGs. Missing and orphan SVGs and PNGs fail on every platform. The Java virtual machine uses Coordinated Universal Time (UTC) and the en_US.UTF-8 locale.

The pinned Java runtime supports Linux and macOS on x86_64 and arm64.

Treat workspace sources as trusted input. Java HTTP(S) requests use an unreachable proxy and PlantUML uses its sandbox profile, but `!script`, `!plugin`, and absolute `!include` directives can access the host. The pinned command-line tool doesn't expose a restricted mode for validation, inspection, or export. Use only trusted workspace sources and imports. Name the source `workspace.dsl`.

The macro reference at `docs/reference/structurizr.md` documents inspection severities, imported inputs, tool pins, and overrides.
