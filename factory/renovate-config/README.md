# `renovate-config`

The shared Renovate presets for this repository, and the tests that check them against the Renovate workflow. The root `renovate.json5` extends the presets from this folder, and the root workflow `.github/workflows/renovate.yaml` runs a self-hosted Renovate on a GitHub App token.

This project came from the `tbhb-dev/renovate-config` repository at commit `6fc408cfec75871cd01b04bf1396e4f82c7fc792`. `IMPORT-MAP.toml` lists every file of that commit as copied, dropped, or rewritten. The earlier history stays in that repository's archive.

## Contents

| Path | Purpose |
| --- | --- |
| `default.json5` | Every repository: best-practices, a 5-day cooldown, ignore paths, grouping, Vale and preset-pin managers, `platformCommit`, and PR titles that fit the 72-character squash subject. |
| `bazel.json5` | Bazel roots: the `bazel` manager for `MODULE.bazel` and `*.bzl` files, and the Bazel version group with its one post-upgrade task. A `customDatasources` entry covers dugite. |
| `go.json5` | Go roots: `gomodTidy` plus module and toolchain groups. |
| `python.json5` | uv projects: `pyproject.toml`, `uv.lock`, and the uv version pin. |
| `ansible.json5` | Roots that pin Galaxy collections with `tbhb_rules_ansible`. A collection bump rewrites `requirements.lock.json` in the same branch. |
| `cloudflare-worker.json5` | Worker projects: a `tbhb_rules` bump regenerates `worker-configuration.d.ts` in the same branch. |
| `rules-cloudflare.json5` | Used by the rules project alone. A bump of wrangler or its group regenerates both smoke Workers' `worker-configuration.d.ts` in the same branch. |
| `renovate_*_test.py` | Tests for the presets and for the inline scripts of the root workflow. Each has a `renovate_*_test.sh` wrapper and a Bazel target in `BUILD.bazel`. |

The root `renovate.json5` extends `default.json5`, `bazel.json5`, `go.json5`, and `python.json5`. The other three presets wait for the manifest each one acts on.

## Rules for the presets

- Only repository-level options belong in a preset. Global-only settings (identity, limits, allowed commands, host rules with tokens) live in the root workflow's `env`, where a pull request can't widen them.
- A preset reference must end in `.json5`. The presets in this folder extend only the built-in presets of Renovate, so resolution stays one hop deep.
- A rule that sets another `groupName` must set `rebaseWhen: "conflicted"` too, because matching rules merge field by field. `renovate_grouping_test.py` checks it.
- Every post-upgrade command in a preset must match exactly one anchored regular expression in the workflow's `RENOVATE_ALLOWED_COMMANDS`, and every regular expression must match a command. `renovate_allowlist_test.py` checks both directions.

## The root workflow

The workflow runs on each push to `main`, on the schedule, and on demand. It checks the log of each run, and it fails on a missing, empty, invalid, or incomplete log, on an error or fatal entry, and on the known failures `Error updating branch`, `artifactErrors`, and `Failed to update MODULE.bazel.lock`.

Before Renovate starts, the workflow audits the active rules on `main` with the Renovate App token. It requires at least one approving review and a status-check rule, and every applied ruleset must report that the App can never bypass it. Missing protection or a failed API check stops the run.

Each run creates its own App token, scoped to this repository, with only the permissions Renovate needs. Commits go through the GitHub API, so GitHub signs them. `allowedUnsafeExecutions` allows only `bazelModDeps`, which leaves `mise` in safe mode. The reviews App approves updates that ran commands only when the diff touches manifests, lock files, and the generated file a task names in `fileFilters`.

## What isn't here

The old repository's reusable workflows and the tools around consumer pins (the validator workflow, its pin-range and pin-consistency tests, and the pin-bump script) don't come along. This repository pins no commit of the old repository, so there are no pins for them to check. A CI gate that runs `renovate-config-validator` over the presets and the root `renovate.json5` is a separate change.
