# `third_party/`

Vendored third-party code, schemas, and external patches live under `third_party/`.
Both `lib/` and `factory/` (and `products/` via `lib/` or explicit allowlists) may consume `third_party/`. Products depend on `lib/` only and must not use `factory/` internals. The factory must not depend on a product.
