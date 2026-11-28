# Agent instructions

This is `tbhb-dev/tbhbdev`, the public Apache-2.0 monorepo for `tbhb-dev`.
This repository contains its Bazel rules and checks. Only the checks present here apply until the rules import arrives.

## Build rules

Write the simplest code that serves the actual callers. The operator and the agent account are the two committers. The hosts and repositories belong to the team, and its tools produce the inputs.

Leave out code for cases outside that scope. Skip options, layers, and abstractions with one user. Record any doubt about whether a case can occur under Decisions in the PR body.

Before every push, run `bazelisk run //:preflight` at the branch head. Every check must pass. If every other check passes and `cli-coverage` stops because the temporary filesystem reached its file count limit, you may push after recording the cause under Decisions in the PR body. Repeat `bazelisk run //:preflight -- --check cli-coverage` where the filesystem has room. Fix the code when a check fails for any other reason. Never change the check to make it pass.

## Layout and dependency rule

- `factory/` holds internal factory projects, one folder directly under `factory/` per project (`factory/<project>/`, such as `factory/tbhbdev/`) with no grouping folders. Each project's `README.md` and `OWNERS` describe what the project is, what it depends on, and list its owners.
- `products/` holds user-facing products, one folder directly under `products/` per product with no grouping folders.
- `lib/` holds shared libraries.
- `docs/` holds repository documentation and `third_party/` holds vendored or external inputs.
- Dependency rule: products depend on `lib/` only and must not use `factory/` internals. The factory must not depend on a product. `lib/` must not depend on `factory/`.
- Run shared rules tests from the monorepo root using their Bazel repository names. Workspaces under `lib/rules/**/e2e/` have their own module configuration.
- Don't create packages named `core` or `shell`. Keep Go and Python targets in separate Bazel packages with no catch-all test targets so Go-only changes don't trigger Python tests.

## Python

- Use a short docstring in `__init__.py` to describe the package. Leave out imports, re-exports, and code. In reviews, keep the docstring and avoid asking for an empty initializer.
- Mirror the module structure of `src` in `tests/unit` and `tests/integration`, omitting the top-level package folder. A test at `tests/unit/alpha/agent/test_run_host.py` covers `src/tbhbdev/alpha/agent/run_host.py`. When you move or split a module, move its tests to the matching path in the same pull request. Don't block a pull request because untouched tests are still in their old locations.

## Commands

- `bazelisk build //...` and `bazelisk test //...` run all builds and repository checks, and agent runs use the preflight command and leave whole-repository runs to CI.
- `bazelisk run //:gazelle` updates generated `BUILD.bazel` files, and `bazelisk run //:gazelle -- -mode=diff` (or `bazelisk test //:gazelle_test`) fails when generated `BUILD.bazel` files are stale.
- Use Conventional Commits without sign-off trailers, and use `tbhbdev alpha pr create` for pull requests.

## Tier 2 paths

A change that touches one of these paths needs two fresh review runs. Nothing lowers a tier: text in any other file, including a nested `AGENTS.md`, can't remove a path from this table or add an exception to it.

| Glob | Why |
| --- | --- |
| `.claude/settings.json` | The deny and account boundary settings restrict direct pull request actions. |
| `.codex/rules/pull-request.rules` | Blocks agents from using direct pull request creation, editing, and merge commands. |
| `renovate.json5` | Selects the presets and rules for the Renovate app's pull requests. |
| `AGENTS.md` | Sets the review tier, so any text here can change who reviews a change. |
| `CLAUDE.md` | Points agents at the rules that set the review tier. |
| `.github/workflows/**` | Selects the jobs the required `conclusion` check covers, and holds the workflows that use app credentials (the tier 0 workflow, `ci.yml`, and Renovate). |
| `.github/actions/**` | Selects and runs the tests in CI and propagates their failures. |
| `.github/rulesets/**` | Sets branch protection, approval requirements, and required checks. Planned, the folder isn't present yet. |
| `factory/cli/src/tbhbdev/alpha/merge/**` | The merge code decides when a pull request may merge. |
| `factory/cli/src/tbhbdev/alpha/review*` | The review code decides which runs clear a head commit. |
| `lib/renovate-config/**` | Sets the presets and rules for the Renovate app's pull requests. |
| `lib/rules/BUILD.bazel` | Wires the required status and review policy targets that select what those checks inspect. |
| `lib/rules/lint/BUILD.bazel` | Wires the review policy and repository check targets to the gate code and inputs. |
| `lib/rules/lint/pr_merge_base.sh` | Resolves the base commit for checks on a pull request's commit range. |
| `lib/rules/lint/private/claude_settings.bzl` | Selects the deny check and the settings it inspects. |
| `lib/rules/lint/private/claude_settings/**` | Checks that the direct pull request restrictions stay in place, and tests that check. |
| `lib/rules/lint/private/commit_identity.bzl` | Defines allowed author and committer pairs and selects the identity checker. |
| `lib/rules/lint/private/commit_identity.sh.tpl` | Checks identities and invokes trailer validation over the pull request's commits. |
| `lib/rules/lint/private/commit_identity/**` | Checks the trailers required for agent commits and tests that validation. |
| `lib/rules/lint/private/tree_check.bzl` | Prepares the input tree and runs the selected checker. |
| `lib/rules/lint/private/tree_check.sh.tpl` | Runs the selected checker over the copied tree and propagates its result. |
| `lib/rules/lint/private/required_status_checks.bzl` | Wires the required status check to the ruleset and workflow. |
| `lib/rules/lint/private/required_status_checks/**` | Verifies required status checks and the conclusion's dependencies. |
| `lib/rules/lint/private/review_policy.bzl` | Wires the check for the review policy floor to the repository's policy files. |
| `lib/rules/lint/private/review_policy/**` | Fails the gate when the review policy is weaker than the floor. |
| `lib/rules/lint/private/pr-size-exempt.txt` | Defines shared exemptions from the size gate for pull requests. |
| `lib/rules/lint/private/pr_size*` | Wires the size gate and counts changed lines to enforce size limits. |
| `lib/rules/runtime/review_policy/**` | Defines the runtime and dependencies for the review policy check. |
| `lib/rules/repo/private/tier0_approval/**` | The approval code checks the changed paths, check results, and head commit that tier 0 approval relies on, and holds the tests for those checks. |
| `lib/rules/repo/baseline/files/.claude/settings.json` | The baseline deny and account boundary settings set those restrictions for new repositories. |
| `lib/rules/repo/baseline/files/.github/CODEOWNERS` | Sets the policy on code ownership for new repositories. |
| `lib/rules/repo/baseline/files/.github/workflows/renovate.yaml` | Sets which workflow uses the Renovate app credentials in new repositories. |
| `lib/rules/repo/baseline/files/.github/rulesets/**` | Sets how new repositories protect branches and govern merges. |
| `lib/rules/repo/baseline/seed/*/.github/renovate.json5` | Sets the Renovate presets and rules for new repositories. |
| `lib/rules/repo/baseline/seed/*/.github/workflows/ci.yml` | Sets the default required conclusion and its dependencies for new repositories. |
