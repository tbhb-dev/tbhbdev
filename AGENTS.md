# Agent instructions

This is `tbhb-dev/tbhbdev`, the public Apache-2.0 monorepo for `tbhb-dev`.
This repository contains its Bazel rules and checks. Only the checks present here apply until the rules import arrives.

## Build rules

Write the simplest code that serves the actual callers. The operator and the agent account are the two committers. The hosts and repositories belong to the team, and its tools produce the inputs.

Leave out code for cases outside that scope. Skip options, layers, and abstractions with one user. Record any doubt about whether a case can occur under Decisions in the PR body.

## Layout and dependency rule

- `factory/` holds internal factory projects, one folder directly under `factory/` per project (`factory/<project>/`, such as `factory/tbhbdev/`) with no grouping folders. Each project's `README.md` and `OWNERS` describe what the project is, what it depends on, and list its owners.
- `products/` holds user-facing products, one folder directly under `products/` per product with no grouping folders.
- `lib/` holds shared libraries.
- `docs/` holds repository documentation and `third_party/` holds vendored or external inputs.
- Dependency rule: products depend on `lib/` only and must not use `factory/` internals. The factory must not depend on a product. `lib/` must not depend on `factory/`.
- Run shared rules tests from the monorepo root using their Bazel repository names. Workspaces under `lib/rules/**/e2e/` have their own module configuration.
- Don't create packages named `core` or `shell`. Keep Go and Python targets in separate Bazel packages with no catch-all test targets so Go-only changes don't trigger Python tests.

## Python

- An `__init__.py` holds only a short docstring that says what the package contains. Leave out imports, re-exports, and code. In reviews, don't ask for the docstring to be removed or for an empty initializer.
- Mirror the module structure of `src` in `tests/unit` and `tests/integration`, omitting the top-level package folder. For example, `tests/unit/alpha/agent/test_run_host.py` tests `src/tbhbdev/alpha/agent/run_host.py`. When you move or split a module, move its tests to the matching path in the same pull request. Don't block a pull request because untouched tests are still in their old locations.

## Commands

- `bazelisk build //...` and `bazelisk test //...` run all builds and repository checks.
- `bazelisk run //:gazelle` updates generated `BUILD.bazel` files, and `bazelisk run //:gazelle -- -mode=diff` (or `bazelisk test //:gazelle_test`) fails when generated `BUILD.bazel` files are stale.
- Use Conventional Commits without sign-off trailers, and use `tbhbdev alpha pr create` for pull requests.
