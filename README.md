# tbhbdev

Public monorepo for `tbhb-dev` (`factory/`, `products/`, `lib/`, `docs/`, and `third_party/`), with one folder per project directly under `factory/` and `products/` and no grouping folders.
Products depend on `lib/` only and must not use `factory/` internals. The factory must not depend on a product.
