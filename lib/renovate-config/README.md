# Renovate presets

This directory in `tbhb-dev/tbhbdev` holds the shared Renovate presets and their tests. The monorepo's root `renovate.json5` extends the presets from the default branch. The root workflow runs Renovate with a GitHub App token.

## Use the presets in another repository

In your repository's `renovate.json5`, extend `default.json5` first and add the presets for the tools you use. Replace `<commit-sha>` with the full hash of a commit on `tbhb-dev/tbhbdev` that contains the presets. Use the same hash for each preset. The `// main` comment lets Renovate update the pin as the branch advances.

```json5
{
  extends: [
    "github>tbhb-dev/tbhbdev//lib/renovate-config/default.json5#<commit-sha>", // main
  ],
}
```

## Choose presets

| Preset | Purpose |
| --- | --- |
| `default.json5` | Shared update policy, grouping, and managers for Vale and preset pins. |
| `bazel.json5` | Bazel modules, tool archives, and the Bazel version group. |
| `bazelrc.json5` | Extend after `bazel.json5` if your root defines `//:preset.update` to regenerate `preset.bazelrc`. |
| `go.json5` | Go module and toolchain groups, with `gomodTidy`. |
| `python.json5` | Python projects that use uv. |
| `ansible.json5` | Refresh the Galaxy collection lock on Ansible collection updates. |
| `cloudflare-worker.json5` | Regenerate Worker types on rules updates. |
| `rules-cloudflare.json5` | Regenerate types for both Workers in the rules project's smoke tests. |
| `workflow.json5` | Manage image and tool pins in Renovate workflows. |

The monorepo uses `default.json5`, `bazel.json5`, `go.json5`, `python.json5`, and `workflow.json5`.

## Maintain the presets

- Put global settings such as credentials and allowed commands in the Renovate runner configuration.
- Keep the `.json5` suffix in each preset reference.
- Set `rebaseWhen: "conflicted"` when a rule overrides the pins group with another group. `renovate_grouping_test.py` checks that these rules reset the rebase policy.
- Match each post-upgrade command to one anchored expression in the workflow's `RENOVATE_ALLOWED_COMMANDS`. `renovate_allowlist_test.py` checks the presets and allowlist against each other.

## Root workflow

Renovate runs on pushes to `main`, on a schedule, and on demand. Before running, it checks that the default branch requires a review and status checks, and that the App can't bypass those rules. After running, it checks the log for completion and errors.

Each run uses an App token scoped to this repository. GitHub signs commits made through its API. The runner allows `bazelModDeps` to refresh the Bazel lockfile. Mise runs in safe mode.

## Import history

The presets came from `tbhb-dev/renovate-config` at commit `6fc408cfec75871cd01b04bf1396e4f82c7fc792`. [IMPORT-MAP.toml](IMPORT-MAP.toml) records which files the import copied, rewrote, or dropped.
