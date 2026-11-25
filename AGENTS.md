# Agent instructions

This is `tbhb-dev/tbhbdev`, the public Apache-2.0 monorepo for `tbhb-dev`.
Shared Bazel lint, formatting, and language gates come from [`tbhb-dev/rules`](https://github.com/tbhb-dev/rules) (`@tbhb_rules`, `@tbhb_rules_go`, and `@tbhb_rules_python`).

## Layout and dependency rule

- `factory/` holds internal factory projects, one folder directly under `factory/` per project (`factory/<project>/`, such as `factory/tbhbdev/`), with each project's role recorded in its `README.md` and `OWNERS` rather than in the path.
- `products/` holds user-facing products.
- `lib/` holds shared libraries.
- `docs/` holds repository documentation and `third_party/` holds vendored or external inputs.
- Dependency rule: products depend on `lib/` only, never on `factory/` internals; the factory never depends on a product.
- Do not create packages named `core` or `shell`. Keep Go and Python targets in separate Bazel packages with no catch-all test targets so Go-only changes do not trigger Python tests.

## Commands

- `bazelisk build //...` and `bazelisk test //...` run all builds and repository checks.
- `bazelisk run //:gazelle` updates generated `BUILD.bazel` files, and `bazelisk run //:gazelle -- -mode=diff` (or `bazelisk test //:gazelle_test`) fails when generated `BUILD.bazel` files are stale.
- Use Conventional Commits without sign-off trailers, and use `tbhbdev alpha pr create` for pull requests.
