# tbhbdev

Public monorepo for `tbhb-dev` (`factory/`, `products/`, `lib/`, `docs/`, and `third_party/`), with one folder per project directly under `factory/` and `products/` and no grouping folders.
Products depend on `lib/` only, never on `factory/` internals; the factory never depends on a product.
